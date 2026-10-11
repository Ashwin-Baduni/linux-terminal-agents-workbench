"""Apply a plan as a journaled transaction, and roll one back.

Before anything changes, the journal records each step's previous state (with a copy of
every file it replaces), the state it will leave, and which steps are done. A failure
undoes the finished steps at once. An interruption leaves the journal behind: the next
run refuses to continue until `--rollback` has restored it.
"""
import contextlib
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
        """Run every step; on any failure undo the finished ones and re-raise."""
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
            try:
                restore(self.home, self.journal, self.desktop, automatic=True)
                self.journal['status'] = 'rolled-back'
                self.journal['error'] = f'{type(error).__name__}: {error}'
                self._save()
            except BaseException as second:
                self.journal['status'] = 'failed'
                self.journal['error'] = f'{type(error).__name__}: {error}; undo failed: {second}'
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


def preflight(home, journal, desktop):
    """(steps to undo, problems, left alone) without changing anything."""
    undo, problems, left = [], [], []
    done = journal.get('done', [])
    numbers = list(done)
    # An interrupted run may have finished the step it was on without recording it.
    in_flight = len(done) if journal.get('status') != 'complete' and len(done) < len(journal['steps']) else None
    if in_flight is not None:
        numbers.append(in_flight)
    for number in reversed(numbers):
        step = journal['steps'][number]
        if number == in_flight:
            if step['type'] == 'path':
                try:
                    current = home.state(step['path'])
                except Unsafe:
                    continue
                if current == State.from_dict(step['after']) and current != State.from_dict(step['before']):
                    undo.append(step)
            continue
        if step['type'] == 'path':
            try:
                current = home.state(step['path'])
            except Unsafe as error:
                problems.append(f'~/{step["path"]}: {error}')
                continue
            before, after = State.from_dict(step['before']), State.from_dict(step['after'])
            if current == before:
                continue
            if current == after or (after.kind == 'dir' and current.kind == 'dir'):
                if 'blob' in step:
                    try:
                        _blob(home, step)
                    except (OSError, Unsafe) as error:
                        problems.append(f'~/{step["path"]}: {error}')
                        continue
                undo.append(step)
            elif step.get('keep_if_changed'):
                left.append(f'~/{step["path"]} (changed since installing; it stays)')
            else:
                problems.append(f'~/{step["path"]} changed after installation; review it, then roll back again.')
        elif step['type'] == 'gsetting':
            current = desktop.get_value(step['schema'], step['key']) if desktop else None
            if current is None or desktop.same(current, step['before']):
                continue
            if desktop.same(current, step['after']):
                undo.append(step)
            else:
                problems.append(f'GNOME setting {step["schema"]} {step["key"]} changed since: {current}')
        elif step['type'] == 'unit':
            if desktop:
                undo.append(step)
    return undo, problems, left


def restore(home, journal, desktop, automatic=False):
    undo, problems, left = preflight(home, journal, desktop)
    if problems and not automatic:
        raise RollbackRefused(problems)
    for step in undo:
        if step['type'] == 'path':
            before = State.from_dict(step['before'])
            current = home.state(step['path'])
            if before.kind == 'absent':
                if current.kind == 'dir':
                    if not home.remove(step['path']):
                        left.append(f'~/{step["path"]}/ (not empty)')
                else:
                    home.remove(step['path'])
            elif before.kind == 'file':
                home.write(step['path'], _blob(home, step), before.mode)
            elif before.kind == 'symlink':
                home.symlink(step['path'], before.link)
        elif step['type'] == 'gsetting':
            desktop.set_value(step['schema'], step['key'], step['before'])
        elif step['type'] == 'unit':
            desktop.restore_unit(step['unit'], step['before'], step['after'])
    return problems, left


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


def rollback(home, folder, desktop=None):
    """Restore one recorded transaction after checking that nothing conflicts."""
    rel, journal = load_journal(home, folder)
    if journal.get('status') == 'rolled-back':
        return [], ['this installation was already rolled back']
    problems, left = restore(home, journal, desktop)
    journal['status'] = 'rolled-back'
    journal['rolled_back'] = datetime.now(timezone.utc).isoformat()
    home.write(rel / 'journal.json', (json.dumps(journal, indent=2) + '\n').encode(), 0o600)
    return problems, left


def latest(home):
    found = [path for path, journal in journals(home) if journal.get('status') in ('applying', 'failed', 'complete')]
    if not found:
        raise RollbackRefused(['there is no installation to roll back'])
    return found[-1]


__all__ = ['Home', 'Transaction', 'rollback', 'latest', 'unfinished', 'install_lock', 'RollbackRefused', 'Interrupted']
