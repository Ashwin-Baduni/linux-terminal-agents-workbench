"""Work out exactly which paths an installation changes, before anything is changed."""
from dataclasses import dataclass, field
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tempfile

from . import merges
from .fsops import State, Unsafe, relative, sha

PRIVATE = ('claude-settings', 'codex-config')   # new agent settings files are private (0600)
MEMORY_ROOT = 'ai-memory'


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


def text(data):
    return None if data is None else data.decode('utf-8')


def git_ignore_target(context):
    """The global git ignore file git uses for the target user."""
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env['HOME'] = str(context.home.root)
    if not context.real:
        env.pop('XDG_CONFIG_HOME', None)
    configured = subprocess.run(['git', 'config', '--global', '--get', 'core.excludesfile'],
                                capture_output=True, text=True, env=env).stdout.strip()
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
    link = os.path.relpath(str(memory), str(folder))
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
            if state.kind == 'symlink' and state.link == entry.link:
                continue
            if state.kind == 'dir':
                raise PlanError(f'{context.home.root / target} is a folder; a symlink is needed there.')
            why = 'create' if state.kind == 'absent' else 'replace ' + state.kind
            plan.add(Change(target, 'symlink', state, link=entry.link, why=why))
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
            target = git_ignore_target(context)
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
