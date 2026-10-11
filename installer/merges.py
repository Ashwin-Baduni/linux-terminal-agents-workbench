"""Combine the repository's settings with a user's existing files, keeping everything else.

Each strategy takes the existing text (None when there is no file) and the wanted text,
and returns the new text. Running a strategy on its own result returns that result
unchanged, so a repeated installation changes nothing.
"""
import copy
import json
import re
import tomllib


class MergeError(ValueError):
    pass


BEGIN = '# BEGIN LINUX TERMINAL AGENTS WORKBENCH'
END = '# END LINUX TERMINAL AGENTS WORKBENCH'
NOTE = '# Managed by linux-terminal-agents-workbench; reinstalling replaces this block.'
MD_BEGIN = '<!-- BEGIN LINUX TERMINAL AGENTS WORKBENCH -->'
MD_END = '<!-- END LINUX TERMINAL AGENTS WORKBENCH -->'
# Earlier private installers wrapped the same block in "# BEGIN <NAME> TERMINAL SETUP" markers.
LEGACY_MARKED = re.compile(r'^# BEGIN ([A-Z][A-Z ]*) TERMINAL SETUP\n.*?^# END \1 TERMINAL SETUP(?:\n|\Z)', re.S | re.M)
# Before that the block had no markers: it ran from its heading to the workspace prompt hook.
LEGACY_PLAIN = re.compile(r'^# --- Terminal tools\b[^\n]*\n.*?^\tprecmd_functions\+=\(__workspace_prompt\)\nfi(?:\n|\Z)',
                          re.S | re.M)
MEMORY_IMPORT = re.compile(r'^@~/ai-memory/MEMORY\.md\s*$', re.M)


def _block(begin, end, body, note=None):
    lines = [begin] + ([note] if note else []) + [body.rstrip('\n'), end]
    return '\n'.join(lines) + '\n'


def _replace_or_append(text, begin, end, managed):
    """Replace the one managed block in text, or append it."""
    pattern = re.compile(re.escape(begin) + r'\n.*?' + re.escape(end) + r'(?:\n|\Z)', re.S)
    found = pattern.findall(text)
    if len(found) > 1 or text.count(begin) != len(found):
        raise MergeError('the managed block is damaged or appears twice; repair it by hand first.')
    if found:
        return pattern.sub(lambda _: managed, text, count=1)
    if not text.strip():
        return managed
    return text.rstrip('\n') + '\n\n' + managed


def bash_block(existing, wanted):
    """~/.bashrc: one managed block; an earlier marked or unmarked terminal block is replaced once."""
    text = existing or ''
    managed = _block(BEGIN, END, wanted, NOTE)
    if BEGIN in text:
        return _replace_or_append(text, BEGIN, END, managed)
    for pattern in (LEGACY_MARKED, LEGACY_PLAIN):
        found = pattern.findall(text) if pattern is LEGACY_PLAIN else list(pattern.finditer(text))
        if len(found) > 1:
            raise MergeError('~/.bashrc has more than one earlier terminal block; merge them by hand first.')
        if found:
            return pattern.sub(lambda _: managed, text, count=1)
    return _replace_or_append(text, BEGIN, END, managed)


def inputrc(existing, wanted):
    """A missing ~/.inputrc gets the whole file; otherwise its key bindings are added once."""
    if not (existing or '').strip():
        return wanted
    body = re.sub(r'^\$include /etc/inputrc\n+', '', wanted)
    if BEGIN not in existing and body.strip() in existing:
        return existing
    return _replace_or_append(existing, BEGIN, END, _block(BEGIN, END, body, NOTE))


def deep_merge(existing, wanted):
    result = dict(existing)
    for key, value in wanted.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _load_json(existing, what):
    if existing is None or not existing.strip():
        return {}
    try:
        data = json.loads(existing)
    except json.JSONDecodeError as error:
        raise MergeError(f'{what} is not valid JSON ({error}); fix it first.') from error
    if not isinstance(data, dict):
        raise MergeError(f'{what} does not hold a JSON object.')
    return data


def _dump_json(existing, before, after):
    if existing is not None and before == after:
        return existing
    return json.dumps(after, indent=2, ensure_ascii=False) + '\n'


def json_settings(existing, wanted, what='the settings file'):
    before = _load_json(existing, what)
    return _dump_json(existing, before, deep_merge(before, json.loads(wanted)))


def claude_settings(existing, wanted, what='~/.claude/settings.json'):
    """Our keys are set, our hooks added once by command; other keys and hooks stay."""
    before = _load_json(existing, what)
    wanted = json.loads(wanted) if isinstance(wanted, str) else wanted
    hooks = wanted.get('hooks', {})
    result = deep_merge(before, {k: v for k, v in wanted.items() if k != 'hooks'})
    if hooks:
        merged = copy.deepcopy(result.get('hooks') or {})
        if not isinstance(merged, dict):
            raise MergeError(f'{what}: "hooks" is not an object.')
        for event, groups in hooks.items():
            previous = merged.setdefault(event, [])
            for group in groups:
                commands = {h.get('command') for entry in previous for h in entry.get('hooks', [])}
                if not all(h['command'] in commands for h in group['hooks']):
                    previous.append(copy.deepcopy(group))
        result['hooks'] = merged
    return _dump_json(existing, before, result)


def codex_config(existing, wanted, what='~/.codex/config.toml'):
    """Set our scalar values in place; projects, providers, MCP servers and comments stay."""
    text = existing or ''
    wanted = json.loads(wanted) if isinstance(wanted, str) else wanted
    try:
        original = tomllib.loads(text) if text else {}
    except tomllib.TOMLDecodeError as error:
        raise MergeError(f'{what} is not valid TOML ({error}); fix it first.') from error
    expected = copy.deepcopy(original)
    for table, values in wanted.items():
        (expected.setdefault(table, {}) if table else expected).update(values)
    lines = text.splitlines(keepends=True)
    for table, values in wanted.items():
        current, positions, headers = '', {}, {}
        for i, line in enumerate(lines):
            heading = re.match(r'^\s*\[([^\[\]]+)\]\s*(?:#.*)?$', line)
            if heading:
                current = heading[1].strip()
                headers[current] = i
            elif current == table:
                assignment = re.match(r'^\s*([A-Za-z_][A-Za-z0-9_-]*)\s*=', line)
                if assignment:
                    positions[assignment[1]] = i
        missing = []
        for key, value in values.items():
            encoded = f'{key} = {json.dumps(value)}\n'
            if key in positions:
                if (original.get(table, {}) if table else original).get(key) != value:
                    lines[positions[key]] = encoded
            else:
                missing.append(encoded)
        if not missing:
            continue
        if not table:
            lines[0:0] = missing + (['\n'] if lines else [])
        elif table in headers:
            lines[headers[table] + 1:headers[table] + 1] = missing
        else:
            if lines and not lines[-1].endswith('\n'):
                lines[-1] += '\n'
            lines.extend((['\n'] if lines else []) + [f'[{table}]\n', *missing])
    result = ''.join(lines)
    parsed = tomllib.loads(result)
    if parsed != expected:
        raise MergeError(f'{what} has a layout this installer cannot change safely; set these values by hand: '
                         + json.dumps(wanted))
    return result


def lines_first(existing, wanted):
    """Our lines first, in order; the file's other lines after them."""
    first = [line for line in wanted.splitlines() if line.strip()]
    rest = [line for line in (existing or '').splitlines() if line.strip() and line not in first]
    result = '\n'.join(first + rest) + '\n'
    return existing if existing is not None and existing == result else result


def mimeapps(existing, wanted):
    """Default applications for types that have none yet; the user's own choices stay."""
    defaults = json.loads(wanted) if isinstance(wanted, str) else wanted
    lines = (existing or '').splitlines()
    section = next((i for i, line in enumerate(lines) if line.strip() == '[Default Applications]'), None)
    if section is None:
        if lines and lines[-1].strip():
            lines.append('')
        lines.append('[Default Applications]')
        section = len(lines) - 1
    end = next((i for i in range(section + 1, len(lines)) if lines[i].strip().startswith('[')), len(lines))
    present = {line.split('=', 1)[0].strip() for line in lines[section + 1:end] if '=' in line}
    additions = [f'{mime}={app};' for mime, app in defaults.items() if mime not in present]
    if not additions:
        return existing if existing is not None else '\n'.join(lines) + '\n'
    while end > section + 1 and not lines[end - 1].strip():
        end -= 1
    lines[end:end] = additions
    return '\n'.join(lines) + '\n'


def claude_import(existing, wanted):
    """Add the shared-memory import to ~/.claude/CLAUDE.md unless it is already there."""
    if existing and MEMORY_IMPORT.search(existing):
        return existing
    if not (existing or '').strip():
        return wanted
    return existing.rstrip('\n') + '\n\n' + wanted


def lines_once(existing, wanted):
    """Append the wanted lines that are missing (a git ignore list)."""
    have = (existing or '').splitlines()
    missing = [line for line in wanted.splitlines() if line.strip() and line not in have]
    if not missing:
        return existing if existing is not None else ''
    return '\n'.join(have + missing) + '\n'


def memory_reference(existing, wanted):
    """Add the shared-memory reference block to an instructions file that lacks it."""
    if existing and (MD_BEGIN in existing or '~/ai-memory/MEMORY.md' in existing):
        return existing
    return _replace_or_append(existing or '', MD_BEGIN, MD_END, _block(MD_BEGIN, MD_END, wanted))


STRATEGIES = {
    'bash-block': bash_block,
    'inputrc': inputrc,
    'json': json_settings,
    'lines-first': lines_first,
    'mimeapps': mimeapps,
    'claude-settings': claude_settings,
    'codex-config': codex_config,
    'claude-import': claude_import,
    'git-ignore': lines_once,
    'codex-instructions': memory_reference,
}
