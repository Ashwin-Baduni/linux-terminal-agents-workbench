"""Apply a plan as a journaled transaction, and roll one back.

Before anything changes, the journal records each step's previous state (with a copy of
every file it replaces), the state it will leave, and which steps are done. A failure
undoes the finished steps at once. An interruption leaves the journal behind: the next
run refuses to continue until `--rollback` has restored it.
"""
import contextlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path, PurePosixPath

from .fsops import Home, State, Unsafe, sha

STATE_DIR = PurePosixPath('.local/state/workbench')
BACKUPS = STATE_DIR / 'backups'
FORMAT = 1


class RollbackRefused(ValueError):
    def __init__(self, problems):
        super().__init__('Nothing was restored:\n' + '\n'.join('  ' + p for p in problems))
        self.problems = problems


class Interrupted(RuntimeError):
    pass


@contextlib.contextmanager
def install_lock(home):
    """One installer or rollback at a time for a home."""
    home.mkdir(STATE_DIR)
    fd, leaf = home.locate(STATE_DIR / 'install.lock', create=True)
    try:
        handle = os.open(leaf, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=fd)
    finally:
        os.close(fd)
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Interrupted('Another installation or rollback is running for this home.') from None
        yield
    finally:
        os.close(handle)


def journals(home):
    """(folder, journal) for every recorded transaction, oldest first."""
    folder = home.root / BACKUPS
    found = []
    if folder.is_dir() and not folder.is_symlink():
        for path in sorted(folder.iterdir()):
            try:
                found.append((path, json.loads((path / 'journal.json').read_text())))
            except (OSError, ValueError):
                continue
    return found


def unfinished(home):
    return [(path, journal) for path, journal in journals(home) if journal.get('status') in ('applying', 'failed')]


class Transaction:
    def __init__(self, home, plan, desktop=None, meta=None):
        self.home = home
        self.plan = plan
        self.desktop = desktop
        self.meta = meta or {}
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        self.folder = BACKUPS / stamp
        self.journal = None

    def _save(self):
        self.home.write(self.folder / 'journal.json', (json.dumps(self.journal, indent=2) + '\n').encode(), 0o600)

    def prepare(self, gsettings=(), units=()):
        steps = []
        for change in self.plan.changes:
            before = change.before
            step = {'type': 'path', 'path': change.path, 'before': before.as_dict(), 'after': change.after.as_dict(),
                    'why': change.why, 'keep_if_changed': change.keep_if_changed}
            if before.kind == 'file':
                data = self.home.read(change.path)
                if sha(data) != before.sha256:
                    raise Unsafe(f'~/{change.path} changed while the installer was planning; run it again.')
                blob = self.folder / 'files' / before.sha256
                self.home.write(blob, data, 0o600)
                step['blob'] = str(blob)
            steps.append(step)
        for item in gsettings:
            steps.append({'type': 'gsetting', **item})
        for item in units:
            steps.append({'type': 'unit', **item})
        self.journal = {'format': FORMAT, 'created': datetime.now(timezone.utc).isoformat(),
                        'home': str(self.home.root), 'status': 'prepared', 'steps': steps, 'done': [],
                        **self.meta}
        self._save()
        return self.home.root / self.folder

    def apply(self, after_step=None):
        """Run every step; on any failure undo what was done, record the outcome and re-raise."""
        self.journal['status'] = 'applying'
        self._save()
        try:
            for number, step in enumerate(self.journal['steps']):
                self._do(step)
                self.journal['done'].append(number)
                self._save()
                if after_step:
                    after_step(number, step)
        except BaseException as error:
            self.journal['error'] = f'{type(error).__name__}: {error}'
            recovery = {}
            try:
                outcome = restore(self.home, self.journal, self.desktop, automatic=True)
                recovery = {'restored': outcome.restored, 'kept': outcome.left, 'problems': outcome.problems}
                self.journal['status'] = 'failed' if outcome.problems else 'rolled-back'
            except BaseException as second:
                recovery['undo_error'] = f'{type(second).__name__}: {second}'
                self.journal['status'] = 'failed'
            self.journal['recovery'] = recovery
            self._save()
            raise
        self.journal['status'] = 'complete'
        self._save()

    def _do(self, step):
        if step['type'] == 'path':
            change = next(c for c in self.plan.changes if c.path == step['path'])
            current = self.home.state(change.path)
            if current != change.before:
                raise Unsafe(f'~/{change.path} changed after it was backed up; nothing more was changed.')
            if change.kind == 'dir':
                self.home.mkdir(change.path, change.mode)
            elif change.kind == 'symlink':
                self.home.symlink(change.path, change.link)
            else:
                self.home.write(change.path, change.data, change.mode)
        elif step['type'] == 'gsetting':
            self.desktop.set_value(step['schema'], step['key'], step['after'])
        elif step['type'] == 'unit':
            self.desktop.set_unit(step['unit'], step['after'])


def _blob(home, step):
    data = home.read(step['blob'])
    if sha(data) != step['before']['sha256']:
        raise Unsafe(f'the backup of ~/{step["path"]} is damaged ({step["blob"]}).')
    return data


ENABLED = ('enabled', 'enabled-runtime', 'static', 'alias', 'linked', 'linked-runtime')
RUNNING = ('active', 'activating', 'reloading')


@dataclass
class Recovery:
    """What undoing a transaction would do, and what stands in its way."""
    paths: list = field(default_factory=list)       # path steps to restore, last change first
    settings: list = field(default_factory=list)    # GNOME settings to set back
    units: list = field(default_factory=list)       # {unit, stop, disable, restart}
    problems: list = field(default_factory=list)    # changes made since, which a rollback would overwrite
    left: list = field(default_factory=list)        # things deliberately left as they are
    restored: list = field(default_factory=list)

    def items(self):
        return [f'~/{s["path"]}' for s in self.paths] + [f'{s["schema"]} {s["key"]}' for s in self.settings] + \
            [a['unit'] for a in self.units]


def _path(home, step, partial, plan):
    try:
        current = home.state(step['path'])
    except Unsafe as error:
        plan.problems.append(f'~/{step["path"]}: {error}')
        return
    before, after = State.from_dict(step['before']), State.from_dict(step['after'])
    if current == before:
        return
    if current == after or (after.kind == 'dir' and current.kind == 'dir'):
        if 'blob' in step:
            try:
                _blob(home, step)
            except (OSError, Unsafe) as error:
                plan.problems.append(f'~/{step["path"]}: {error}')
                return
        plan.paths.append(step)
    elif step.get('keep_if_changed'):
        plan.left.append(f'~/{step["path"]} (changed since installing; it stays)')
    elif partial:
        plan.left.append(f'~/{step["path"]} (not changed by the interrupted step; left as it is)')
    else:
        plan.problems.append(f'~/{step["path"]} changed after installation')


def _setting(desktop, step, plan):
    name = f'GNOME setting {step["schema"]} {step["key"]}'
    current = desktop.get_value(step['schema'], step['key'])
    if current is None:
        plan.left.append(f'{name} (no longer on this desktop)')
    elif desktop.same(current, step['before']):
        return
    elif desktop.same(current, step['after']):
        plan.settings.append(step)
    else:
        plan.problems.append(f'{name} changed after installation (now {current})')


def _unit(desktop, step, plan):
    unit, before, after = step['unit'], step['before'], step['after']
    current = desktop.unit_state(unit)
    action = {'unit': unit, 'stop': False, 'disable': False, 'restart': False}
    if current['enabled'] != before['enabled']:
        if current['enabled'] == after['enabled'] and before['enabled'] not in ENABLED:
            action['disable'] = True
        elif current['enabled'] != after['enabled']:
            plan.problems.append(f'service {unit} changed after installation (now {current["enabled"]})')
            return
    if before['active'] not in RUNNING:
        action['stop'] = current['active'] in RUNNING
    elif after.get('restart'):
        if current['active'] in RUNNING:
            action['restart'] = True
        else:
            plan.left.append(f'service {unit} (stopped since installing; left stopped)')
    if action['stop'] or action['disable'] or action['restart']:
        plan.units.append(action)


def preflight(home, journal, desktop):
    """Work out a rollback without changing anything."""
    plan = Recovery()
    done = journal.get('done', [])
    numbers = list(done)
    # An interrupted or failed run may have finished part or all of the step it was on.
    in_flight = len(done) if journal.get('status') != 'complete' and len(done) < len(journal['steps']) else None
    if in_flight is not None:
        numbers.append(in_flight)
    for number in reversed(numbers):
        step = journal['steps'][number]
        if step['type'] == 'path':
            _path(home, step, number == in_flight, plan)
        elif desktop is None:
            plan.problems.append(f'{step.get("schema") or step.get("unit")}: needs the desktop session it was installed in')
        elif step['type'] == 'gsetting':
            _setting(desktop, step, plan)
        elif step['type'] == 'unit':
            _unit(desktop, step, plan)
    return plan


def restore(home, journal, desktop, automatic=False, keep_changed=False):
    """Undo a transaction: services stop first, then files and settings return, then services restart.

    Refuses (RollbackRefused) when something changed after installation, unless keep_changed
    leaves those changes in place; an automatic recovery after a failure also leaves them and
    reports them as problems.
    """
    plan = preflight(home, journal, desktop)
    if plan.problems and not (automatic or keep_changed):
        raise RollbackRefused(plan.problems)
    if keep_changed:
        plan.left += [problem + ' (kept)' for problem in plan.problems]
        plan.problems = []
    for action in plan.units:                     # with their current unit files still in place
        if action['stop']:
            desktop.command('systemctl', '--user', 'stop', action['unit'], check=True)
        if action['disable']:
            desktop.command('systemctl', '--user', 'disable', action['unit'], check=True)
    for step in plan.paths:
        before = State.from_dict(step['before'])
        if before.kind == 'absent':
            if home.state(step['path']).kind == 'dir':
                if not home.remove(step['path']):
                    plan.left.append(f'~/{step["path"]}/ (not empty)')
                    continue
            else:
                home.remove(step['path'])
        elif before.kind == 'file':
            home.write(step['path'], _blob(home, step), before.mode)
        elif before.kind == 'symlink':
            home.symlink(step['path'], before.link)
        plan.restored.append(f'~/{step["path"]}')
    for step in plan.settings:
        desktop.set_value(step['schema'], step['key'], step['before'])
        plan.restored.append(f'{step["schema"]} {step["key"]}')
    if plan.units:
        desktop.reload(force=True)                # read the restored unit files
        for action in plan.units:
            if action['restart']:
                desktop.command('systemctl', '--user', 'try-restart', action['unit'], check=True)
            plan.restored.append(action['unit'])
    return plan


def load_journal(home, folder):
    """(relative folder, journal) for a backup folder of this home."""
    folder = Path(folder).expanduser().absolute()
    rel = folder.resolve().relative_to(home.root) if folder.resolve().is_relative_to(home.root) else None
    if rel is None or PurePosixPath(rel).parent != BACKUPS:
        raise RollbackRefused([f'{folder} is not a backup folder of {home.root}'])
    try:
        journal = json.loads(home.read(rel / 'journal.json'))
    except (OSError, ValueError) as error:
        raise RollbackRefused([f'{folder}: no readable journal ({error})']) from error
    if journal.get('home') != str(home.root):
        raise RollbackRefused([f'{folder} belongs to {journal.get("home")}'])
    return rel, journal


def rollback(home, folder, desktop=None, keep_changed=False):
    """Restore one recorded transaction after checking that nothing conflicts; returns the Recovery."""
    rel, journal = load_journal(home, folder)
    if journal.get('status') == 'rolled-back':
        return Recovery(left=['this installation was already rolled back'])
    outcome = restore(home, journal, desktop, keep_changed=keep_changed)
    journal['status'] = 'rolled-back'
    journal['rolled_back'] = datetime.now(timezone.utc).isoformat()
    journal['rollback'] = {'restored': outcome.restored, 'kept': outcome.left}
    home.write(rel / 'journal.json', (json.dumps(journal, indent=2) + '\n').encode(), 0o600)
    return outcome


def latest(home):
    found = [path for path, journal in journals(home) if journal.get('status') in ('applying', 'failed', 'complete')]
    if not found:
        raise RollbackRefused(['there is no installation to roll back'])
    return found[-1]


__all__ = ['Home', 'Transaction', 'rollback', 'latest', 'unfinished', 'install_lock', 'RollbackRefused', 'Interrupted']
