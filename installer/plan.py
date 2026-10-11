"""Work out exactly which paths an installation changes, before anything is changed."""
from dataclasses import dataclass, field
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile

from . import merges
from .fsops import State, Unsafe, relative, sha

PRIVATE = ('claude-settings', 'codex-config')   # new agent settings files are private (0600)
MEMORY_ROOT = 'ai-memory'
UNIT_FOLDER = '.config/systemd/user/'
# Where an administrator masks a user unit for everyone; a file in ~/.config/systemd/user would override it.
SYSTEM_UNIT_DIRS = (Path('/etc/systemd/user'), Path('/run/systemd/user'))


def runtime_unit_dirs(home, env):
    """The user's own other unit folders, where `systemctl --user mask [--runtime]` can also put a mask."""
    folders = [Path(home) / '.config/systemd/user.control']
    if env.get('XDG_RUNTIME_DIR'):
        runtime = Path(env['XDG_RUNTIME_DIR'])
        folders += [runtime / 'systemd/user.control', runtime / 'systemd/user']
    return folders


class PlanError(ValueError):
    pass


@dataclass
class Change:
    path: str
    kind: str                       # file, symlink, dir
    before: State
    data: bytes | None = None
    mode: int | None = None
    link: str | None = None
    why: str = ''
    keep_if_changed: bool = False   # rollback leaves it in place if the user changed it since

    @property
    def after(self):
        if self.kind == 'file':
            return State('file', mode=self.mode, sha256=sha(self.data))
        if self.kind == 'symlink':
            return State('symlink', link=self.link)
        return State('dir', mode=self.mode)


@dataclass
class Plan:
    home: Path
    components: list
    changes: list = field(default_factory=list)
    kept: list = field(default_factory=list)       # (path, reason) left exactly as found
    notes: list = field(default_factory=list)
    masked: set = field(default_factory=set)       # units left alone because they are masked

    def add(self, change):
        self.changes.append(change)


@dataclass
class Context:
    """Facts about the target that decide paths and placeholder values."""
    home: object                    # fsops.Home
    values: dict
    real: bool = False              # installing into the account's own home
    env: dict = field(default_factory=dict)
    agent_policy: str | None = None
    masked: dict = field(default_factory=dict)   # unit -> state systemd reported (real installs and activation)

    def remap(self, target):
        """Honour CODEX_HOME and CLAUDE_CONFIG_DIR on a real installation."""
        for prefix, variable in (('.codex/', 'CODEX_HOME'), ('.claude/', 'CLAUDE_CONFIG_DIR')):
            if self.real and target.startswith(prefix) and self.env.get(variable):
                folder = Path(os.path.expanduser(self.env[variable])).resolve()
                if not folder.is_relative_to(self.home.root):
                    raise PlanError(f'{variable}={folder} is outside {self.home.root}; '
                                    'the installer only changes files inside the home.')
                return str(relative(self.home.root, folder) / target[len(prefix):])
        return target


def link_target(target, link):
    """The home path a manifest link means: its `link` read from the target's own folder."""
    return relative('/', os.path.normpath(PurePosixPath(target).parent / link))


def mask_of(context, target):
    """Why the unit file at target must not be installed (a user or system mask), or None."""
    name = PurePosixPath(target).name
    state = context.home.state(target)
    if state.kind == 'symlink' and os.path.realpath(context.home.root / target) == os.devnull:
        return 'you masked it: its unit file links to /dev/null'
    if name in context.masked:
        return f'systemd reports it as {context.masked[name]}'
    if context.real:
        for folder in (*SYSTEM_UNIT_DIRS, *runtime_unit_dirs(context.home.root, context.env)):
            path = folder / name
            if path.is_symlink() and os.path.realpath(path) == os.devnull:
                return f'it is masked in {folder}, and a unit file in ~/.config/systemd/user must not undo that'
    return None


def masked_note(name, reason):
    return (f'{name} is masked ({reason}), so the installer leaves it alone: no unit file, enabling or starting. '
            f'Unmask it and run the installer again to add it.')


def text(data):
    return None if data is None else data.decode('utf-8')


def configured_excludes(home, env):
    """core.excludesfile read straight from the user's git config files, for when git is not installed yet.
    Later files win, as in git: $XDG_CONFIG_HOME/git/config, then ~/.gitconfig."""
    value, section = '', ''
    for path in (Path(env.get('XDG_CONFIG_HOME') or home / '.config') / 'git/config', home / '.gitconfig'):
        try:
            lines = path.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.split('#', 1)[0].split(';', 1)[0].strip()
            heading = re.match(r'^\[\s*([A-Za-z0-9.-]+)', line)
            if heading:
                section = heading[1].lower()
            elif section == 'core':
                found = re.match(r'(?i)^excludesfile\s*=\s*(.*)$', line)
                if found:
                    value = found[1].strip().strip('"')
    return value


def git_ignore_target(context, plan):
    """The global git ignore file git uses for the target user."""
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env['HOME'] = str(context.home.root)
    if not context.real:
        env.pop('XDG_CONFIG_HOME', None)
    if shutil.which('git', path=env.get('PATH')):
        configured = subprocess.run(['git', 'config', '--global', '--get', 'core.excludesfile'],
                                    capture_output=True, text=True, env=env).stdout.strip()
    else:
        configured = configured_excludes(context.home.root, env)
        plan.notes.append('git is not installed yet, so its global ignore file was read from the git config files; '
                          'the plan is made again with git once the packages are installed.')
    if configured:
        path = Path(configured.replace('~', str(context.home.root), 1) if configured.startswith('~') else configured)
    else:
        base = Path(env.get('XDG_CONFIG_HOME') or context.home.root / '.config')
        path = base / 'git/ignore'
    path = path if path.is_absolute() else context.home.root / path
    if not path.resolve().is_relative_to(context.home.root):
        raise PlanError(f'git uses {path} for ignores, outside the home; add {merges_ignored()} there by hand.')
    return str(relative(context.home.root, path))


def merges_ignored():
    return '.ai-memory and **/.claude/settings.local.json'


def file_change(context, plan, rel, data, wanted_mode, why_new, keep_if_changed=False, preserve_mode=True):
    """Add a regular-file change unless rel already holds exactly this content."""
    before = context.home.state(rel)
    if before.kind == 'dir':
        raise PlanError(f'{context.home.root / rel} is a folder; a file is needed there.')
    if before.kind == 'other':
        raise PlanError(f'{context.home.root / rel} is not a regular file; move it away first.')
    if before.kind == 'file':
        mode = (before.mode | (wanted_mode & 0o111)) if preserve_mode else wanted_mode
        if before.sha256 == sha(data) and before.mode == mode:
            return None
        why = 'update' if before.sha256 != sha(data) else 'fix permissions'
    else:
        mode = wanted_mode
        why = why_new if before.kind == 'absent' else f'replace the symlink (was → {before.link}; its target is not changed)'
    change = Change(rel, 'file', before, data=data, mode=mode, why=why, keep_if_changed=keep_if_changed)
    plan.add(change)
    return change


def plan_codex_instructions(context, plan, entry, target, block):
    """AGENTS.md links to the memory when absent; existing instructions gain a reference block."""
    folder = PurePosixPath(target).parent
    memory = PurePosixPath(MEMORY_ROOT) / 'MEMORY.md'
    link = context.home.link_text(target, memory)
    agents = context.home.state(target)
    override = str(folder / 'AGENTS.override.md')
    if agents.kind == 'absent':
        plan.add(Change(target, 'symlink', agents, link=link, why='link to the shared memory'))
    elif agents.kind == 'symlink' and (context.home.root / folder / agents.link).resolve() == \
            (context.home.root / memory).resolve():
        pass
    else:
        existing = text(context.home.read_followed(target))
        if existing is None:
            raise PlanError(f'{context.home.root / target} cannot be read; fix it first.')
        new = merges.memory_reference(existing, block)
        if new == existing:
            plan.kept.append((target, 'already refers to the shared memory'))
        else:
            change = file_change(context, plan, target, new.encode(), agents.mode or 0o644, 'add memory reference')
            if agents.kind != 'symlink':
                change.why = 'add memory reference'
    state = context.home.state(override)
    if state.kind != 'absent':
        existing = text(context.home.read_followed(override)) or ''
        new = merges.memory_reference(existing, block)
        if new != existing:
            file_change(context, plan, override, new.encode(), state.mode or 0o644,
                        'add the memory reference (Codex reads AGENTS.override.md first)')
        else:
            plan.kept.append((override, 'already refers to the shared memory'))


def compiled_schema(xml_by_name):
    """glib-compile-schemas output for the given schema files, or None when the tool is missing."""
    tool = shutil.which('glib-compile-schemas')
    if not tool:
        return None
    with tempfile.TemporaryDirectory(prefix='workbench-schemas-') as folder:
        for name, data in xml_by_name.items():
            Path(folder, name).write_bytes(data)
        subprocess.run([tool, '--strict', folder], check=True, capture_output=True, text=True)
        return Path(folder, 'gschemas.compiled').read_bytes()


def build(manifest, context, components=None):
    chosen = manifest.closure(components or manifest.default)
    plan = Plan(home=context.home.root, components=chosen)
    if context.agent_policy and 'agents' not in chosen:
        raise PlanError('--agent-policy needs the agents component.')
    entries = manifest.select(chosen)
    merged = {}
    for entry in entries:
        target = context.remap(entry.target)
        if entry.kind == 'merge':
            merged.setdefault(target, []).append(entry)
            continue
        state = context.home.state(target)
        if entry.kind == 'dir':
            if state.kind == 'absent':
                plan.add(Change(target, 'dir', state, mode=0o755, why='create', keep_if_changed=True))
            elif state.kind != 'dir' and not (state.kind == 'symlink' and (context.home.root / target).is_dir()):
                raise PlanError(f'{context.home.root / target} should be a folder.')
            continue
        if entry.kind == 'link':
            link = context.home.link_text(target, link_target(target, entry.link))
            if state.kind == 'symlink' and state.link == link:
                continue
            if state.kind == 'dir':
                raise PlanError(f'{context.home.root / target} is a folder; a symlink is needed there.')
            why = 'create' if state.kind == 'absent' else 'replace ' + state.kind
            plan.add(Change(target, 'symlink', state, link=link, why=why))
            continue
        if target.startswith(UNIT_FOLDER):
            reason = mask_of(context, target)
            if reason:
                name = PurePosixPath(target).name
                plan.masked.add(name)
                plan.kept.append((target, 'masked; left as it is'))
                plan.notes.append(masked_note(name, reason))
                continue
        data = manifest.render(entry, context.values)
        if entry.kind == 'seed':
            others = [p for p in entry.skip_if if context.home.state(p).kind != 'absent']
            if state.kind != 'absent' or others:
                plan.kept.append((target, 'already exists' if state.kind != 'absent' else f'{others[0]} exists'))
                continue
            # A seeded file belongs to the user (or the tool that owns it) once it exists.
            plan.add(Change(target, 'file', state, data=data, mode=entry.mode, why='create', keep_if_changed=True))
            continue
        file_change(context, plan, target, data, entry.mode, 'create')
    for target, group in merged.items():
        strategy = group[0].strategy
        if strategy == 'codex-instructions':
            plan_codex_instructions(context, plan, group[0], target, text(manifest.render(group[0], context.values)))
            continue
        if strategy == 'git-ignore':
            target = git_ignore_target(context, plan)
        state = context.home.state(target)
        if state.kind in ('dir', 'other'):
            raise PlanError(f'{context.home.root / target} is not a regular file; move it away first.')
        existing = text(context.home.read_followed(target))
        if state.kind == 'symlink' and existing is None:
            raise PlanError(f'{context.home.root / target} is a broken symlink; remove or fix it first.')
        new = existing
        try:
            for entry in group:
                new = merges.STRATEGIES[entry.strategy](new, text(manifest.render(entry, context.values)))
            if context.agent_policy and strategy in ('claude-settings', 'codex-config'):
                preset = json.loads((manifest.repo / manifest.components['agents']['presets'][context.agent_policy]).read_text())
                part = preset['claude' if strategy == 'claude-settings' else 'codex']
                new = merges.STRATEGIES[strategy](new, json.dumps(part))
        except merges.MergeError as error:
            raise PlanError(f'{context.home.root / target}: {error}') from error
        if new == existing:
            continue
        mode = 0o600 if strategy in PRIVATE else group[0].mode
        change = file_change(context, plan, target, new.encode(), state.mode if state.kind == 'file' else mode,
                             'create')
        if change and state.kind == 'file':
            change.why = 'merge'
    if 'clipboard' in chosen:
        plan_compiled_schema(manifest, context, plan)
    add_folders(context, plan)
    return plan


def plan_compiled_schema(manifest, context, plan):
    uuid = manifest.components['clipboard']['extension']
    folder = f'.local/share/gnome-shell/extensions/{uuid}/schemas'
    sources = {}
    for entry in manifest.select(['clipboard']):
        if entry.target.startswith(folder + '/') and entry.target.endswith('.gschema.xml'):
            sources[PurePosixPath(entry.target).name] = manifest.render(entry, context.values)
    data = compiled_schema(sources)
    if data is None:
        plan.notes.append('glib-compile-schemas is missing, so the clipboard settings schema was not compiled.')
        return
    file_change(context, plan, f'{folder}/gschemas.compiled', data, 0o644, 'compile')


def add_folders(context, plan):
    """Folders the changes need, created first and removed again by a rollback when empty."""
    planned, folders = {c.path for c in plan.changes if c.kind == 'dir'}, []
    for change in list(plan.changes):
        for folder in context.home.missing_folders(change.path):
            if folder not in planned:
                planned.add(folder)
                folders.append(Change(folder, 'dir', State('absent'), mode=0o755, why='create',
                                      keep_if_changed=True))
    plan.changes[0:0] = folders


def describe(plan):
    lines, folders = [], 0
    for change in plan.changes:
        if change.kind == 'dir' and change.before.kind == 'absent' and not change.path.startswith(MEMORY_ROOT):
            folders += 1
            continue
        label = {'file': '', 'symlink': ' (symlink)', 'dir': '/'}[change.kind]
        lines.append(f'  {change.why:<14} ~/{change.path}{label}')
    if folders:
        lines.append(f'  create         {folders} folders for these files')
    return lines


def ensure_safe(plan, home):
    """Check every planned path once more right before applying."""
    for change in plan.changes:
        try:
            home.missing_folders(change.path)
        except Unsafe as error:
            raise PlanError(str(error)) from error
