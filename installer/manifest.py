"""Read manifests/*.toml: the components, every file they install, and where it goes."""
from dataclasses import dataclass, field
import fnmatch
from pathlib import Path, PurePosixPath
import re
import tomllib

REPO = Path(__file__).resolve().parent.parent
KINDS = ('file', 'seed', 'link', 'dir', 'merge')
STRATEGIES = ('bash-block', 'inputrc', 'json', 'lines-first', 'mimeapps', 'claude-settings',
              'codex-config', 'claude-import', 'git-ignore')
# Everything under these folders must be claimed by a manifest entry.
SHIPPED = ('payload', 'vendor', 'templates', 'licenses')
TOKEN = re.compile(rb'@([A-Z][A-Z0-9_]*)@')


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class Entry:
    component: str
    kind: str
    target: str
    source: str | None = None
    mode: int = 0o644
    render: bool = False
    strategy: str | None = None
    link: str | None = None
    live: str | None = None
    sanitize: tuple = ()


@dataclass
class Manifest:
    repo: Path
    components: dict
    default: list
    supported: dict
    placeholders: dict
    entries: list = field(default_factory=list)

    def closure(self, names):
        """The named components plus everything they require, in manifest order."""
        wanted, pending = set(), list(names)
        while pending:
            name = pending.pop()
            if name not in self.components:
                raise ManifestError(f'Unknown component "{name}". Choose from: ' + ', '.join(self.components))
            if name not in wanted:
                wanted.add(name)
                pending += self.components[name].get('requires', [])
        return [name for name in self.components if name in wanted]

    def select(self, names=None):
        chosen = set(self.closure(names or self.default))
        return [entry for entry in self.entries if entry.component in chosen]

    def render(self, entry, values):
        """The bytes to install for an entry, with its placeholders filled in."""
        data = (self.repo / entry.source).read_bytes()
        if not entry.render:
            return data
        missing = set(self.placeholders) - set(values)
        if missing:
            raise ManifestError('No value for placeholder(s): ' + ', '.join(sorted(missing)))
        for name, value in values.items():
            data = data.replace(b'@' + name.encode() + b'@', str(value).encode())
        return data


def relative(path, what):
    pure = PurePosixPath(path)
    if not path or pure.is_absolute() or '..' in pure.parts:
        raise ManifestError(f'{what} must be a relative path inside the repository or home: {path!r}')
    return str(pure)


def entry_from(raw, components, where):
    kind = raw.get('kind', 'file')
    if kind not in KINDS:
        raise ManifestError(f'{where}: unknown kind "{kind}"')
    if raw.get('component') not in components:
        raise ManifestError(f'{where}: unknown component {raw.get("component")!r}')
    target = relative(raw.get('target', ''), f'{where} target')
    source = raw.get('source')
    if kind in ('link', 'dir'):
        if source:
            raise ManifestError(f'{where}: a {kind} has no source')
    else:
        source = relative(source or '', f'{where} source')
    link = raw.get('link')
    if kind == 'link':
        if not link or PurePosixPath(link).is_absolute():
            raise ManifestError(f'{where}: a link needs a relative "link" value')
    strategy = raw.get('strategy')
    if (kind == 'merge') != (strategy is not None) or (strategy and strategy not in STRATEGIES):
        raise ManifestError(f'{where}: merges, and only merges, name a strategy from ' + ', '.join(STRATEGIES))
    live = raw.get('live', target)
    if live is not False:
        live = relative(live, f'{where} live')
    return Entry(component=raw['component'], kind=kind, target=target, source=source,
                 mode=int(str(raw.get('mode', '644')), 8), render=bool(raw.get('render', False)),
                 strategy=strategy, link=link, live=live or None,
                 sanitize=tuple(tuple(pair) for pair in raw.get('sanitize', [])))


def expand_tree(repo, raw, components, where):
    """One file entry per repository file a [[tree]] includes."""
    source = relative(raw.get('source', ''), f'{where} source')
    target = relative(raw.get('target', ''), f'{where} target')
    root = repo / source
    if not root.is_dir():
        raise ManifestError(f'{where}: {source} is not a directory')
    files = sorted(str(p.relative_to(root)) for p in root.rglob('*') if p.is_file())
    chosen = [name for name in files if any(fnmatch.fnmatchcase(name, pattern) for pattern in raw['include'])]
    for pattern in raw['include']:
        if not any(fnmatch.fnmatchcase(name, pattern) for name in files):
            raise ManifestError(f'{where}: nothing in {source} matches {pattern!r}')
    entries = []
    for name in chosen:
        mode = raw.get('mode') or ('755' if (root / name).stat().st_mode & 0o100 else '644')
        live = raw.get('live', target)
        entries.append(entry_from({'component': raw['component'], 'source': f'{source}/{name}',
                                   'target': f'{target}/{name}', 'mode': mode,
                                   'live': f'{live}/{name}' if live is not False else False},
                                  components, f'{where} ({name})'))
    return entries


def load(repo=REPO):
    repo = Path(repo)
    parts = tomllib.loads((repo / 'manifests/components.toml').read_text())
    files = tomllib.loads((repo / 'manifests/files.toml').read_text())
    if parts.get('format') != 1 or files.get('format') != 1:
        raise ManifestError('This installer reads manifest format 1.')
    components = parts['component']
    manifest = Manifest(repo=repo, components=components, default=parts['default'],
                        supported=parts['supported'], placeholders=files.get('placeholders', {}))
    for name, component in components.items():
        for required in component.get('requires', []):
            if required not in components:
                raise ManifestError(f'Component {name} requires unknown component {required}')
    manifest.closure(manifest.default)
    for number, raw in enumerate(files.get('file', []), 1):
        manifest.entries.append(entry_from(raw, components, f'file #{number}'))
    for number, raw in enumerate(files.get('tree', []), 1):
        manifest.entries.extend(expand_tree(repo, raw, components, f'tree #{number}'))
    return manifest


def problems(manifest):
    """Everything wrong with the manifest as it stands in the repository; empty when it is sound."""
    found, claimed, targets = [], set(), {}
    repo = manifest.repo
    for name, component in manifest.components.items():
        for reference in [component.get('gsettings'), *component.get('presets', {}).values()]:
            if reference:
                claimed.add(reference)
                if not (repo / reference).is_file():
                    found.append(f'component {name}: {reference} is missing')
    for entry in manifest.entries:
        if entry.source:
            claimed.add(entry.source)
            path = repo / entry.source
            if not path.is_file() or path.is_symlink():
                found.append(f'{entry.source}: listed but not a regular file in the repository')
                continue
            data = path.read_bytes()
            tokens = {m.decode() for m in TOKEN.findall(data)}
            unknown = tokens - set(manifest.placeholders)
            if entry.render and unknown:
                found.append(f'{entry.source}: unknown placeholder(s) ' + ', '.join(sorted(unknown)))
            if entry.render and not tokens:
                found.append(f'{entry.source}: render = true but it has no placeholder')
            if not entry.render and tokens & set(manifest.placeholders):
                found.append(f'{entry.source}: has placeholders but is not rendered')
        if entry.kind == 'merge':
            previous = targets.get(entry.target)
            if previous and previous != ('merge', entry.strategy):
                found.append(f'{entry.target}: merged with two different strategies')
            targets[entry.target] = ('merge', entry.strategy)
        else:
            if entry.target in targets:
                found.append(f'{entry.target}: installed by more than one entry')
            targets[entry.target] = (entry.kind, None)
    for folder in SHIPPED:
        for path in sorted((repo / folder).rglob('*')) if (repo / folder).is_dir() else []:
            name = str(path.relative_to(repo))
            if path.is_symlink():
                found.append(f'{name}: symlinks are created by link entries, not committed')
            elif path.is_file() and name not in claimed and not name.endswith('/README.md') \
                    and '__pycache__' not in path.parts:
                found.append(f'{name}: not installed by any manifest entry')
    return found
