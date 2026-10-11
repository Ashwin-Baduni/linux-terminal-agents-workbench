#!/usr/bin/env python3
"""Check that the repository is safe to publish: no private names, paths, secrets or state.

  scripts/check-public.py                    files git would commit (tracked + new, not ignored)
  scripts/check-public.py --history          also every file version in the git history
  scripts/check-public.py --patterns FILE    extra private patterns, one regular expression per line

Patterns built from the machine running the check are always added: its home folder,
user name, host name, and git identity. Keep other private names (people, companies,
projects) in a patterns file outside the repository, so the check itself never
publishes them. Reviewed exceptions live in manifests/public-allowlist.toml.

This is a pattern scan. It catches the usual mistakes, not every possible secret.
"""
import argparse
import fnmatch
import os
from pathlib import Path
import pwd
import re
import socket
import subprocess
import sys
import tomllib

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from installer.manifest import load, problems  # noqa: E402

SECRETS = {
    'private key': r'-----BEGIN [A-Z ]*PRIVATE KEY-----',
    'AWS access key': r'\bAKIA[0-9A-Z]{16}\b',
    'GitHub token': r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})',
    'Anthropic key': r'\bsk-ant-[A-Za-z0-9_-]{20,}',
    'OpenAI-style key': r'\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}',
    'Slack token': r'\bxox[abprs]-[A-Za-z0-9-]{10,}',
    'Google API key': r'\bAIza[0-9A-Za-z_-]{35}\b',
    'JSON web token': r'\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}',
    'assigned secret': r'(?i)\b(?:api[_-]?key|secret|token|passw(?:or)?d)\b["\']?\s*[:=]\s*["\'][^"\'\s]{12,}["\']',
}
EMAIL = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}')
SAFE_EMAIL = re.compile(r'@(?:example\.(?:com|org|net)|localhost)$|^noreply@')
UUID = re.compile(r'(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b')
HOME_PATH = re.compile(r'/home/([A-Za-z0-9_.-]+)')
EXAMPLE_USERS = {'x', 'user', 'example', 'someone', 'you'}
# Files that are personal state, credentials or build leftovers, wherever they appear.
FORBIDDEN_FILES = ['*.pem', '*.key', 'id_rsa*', 'id_ed25519*', '.env', '.env.*', 'auth.json', '.credentials.json',
                   '.claude.json', 'models_cache.json', '*.sqlite', '*.sqlite3', '*.db', '*history*', '*.jsonl',
                   '*.pyc', '.ai-memory', 'settings.local.json', 'ultimate-*.md', '*.bak', '*.bak-*', '*~', '*.orig',
                   '*.swp', 'gschemas.compiled', 'projects.json', 'layout-metadata.json']
FORBIDDEN_PARTS = {'__pycache__', 'node_modules', '.cache', 'resurrect', 'sessions'}
MAX_BYTES = 1_000_000
ALLOWLIST = 'manifests/public-allowlist.toml'


def machine_patterns():
    """Private names of the machine running the check, without writing them anywhere."""
    found = {}
    account = pwd.getpwuid(os.getuid())
    found['this home folder'] = re.escape(account.pw_dir.rstrip('/')) + r'(?![A-Za-z0-9_.-])'
    if len(account.pw_name) >= 3:
        found['this user name'] = r'(?i)(?<![A-Za-z0-9])' + re.escape(account.pw_name) + r'(?![A-Za-z0-9])'
    host = socket.gethostname().split('.')[0]
    if len(host) >= 3 and host.lower() not in ('localhost', 'ubuntu'):
        found['this host name'] = r'(?i)' + re.escape(host)
    for key in ('user.name', 'user.email'):
        value = subprocess.run(['git', 'config', '--global', key], capture_output=True, text=True).stdout.strip()
        if len(value) >= 3:
            found['git ' + key] = r'(?i)' + re.escape(value)
    return found


def committable():
    """The files the next commit would contain: tracked plus new files git does not ignore."""
    listing = subprocess.run(['git', '-C', str(REPO), 'ls-files', '-z', '--cached', '--others', '--exclude-standard'],
                             capture_output=True, text=True, check=True).stdout
    return sorted({name for name in listing.split('\0') if name and ((REPO / name).exists() or (REPO / name).is_symlink())})


def history():
    """(label, bytes) for every file version reachable from any ref."""
    objects = subprocess.run(['git', '-C', str(REPO), 'rev-list', '--objects', '--all'],
                             capture_output=True, text=True).stdout.splitlines()
    for line in objects:
        sha, _, name = line.partition(' ')
        if not name:
            continue
        kind = subprocess.run(['git', '-C', str(REPO), 'cat-file', '-t', sha], capture_output=True, text=True).stdout.strip()
        if kind == 'blob':
            data = subprocess.run(['git', '-C', str(REPO), 'cat-file', 'blob', sha], capture_output=True).stdout
            yield f'{name} (history {sha[:10]})', name, data


def allowed(allowlist, name, value):
    return any((fnmatch.fnmatchcase(name, item['path']) or name == ALLOWLIST) and item['value'] == value
               for item in allowlist)


def scan_text(name, path_for_rules, data, patterns, allowlist):
    findings = []
    if b'\0' in data[:8192]:
        return findings
    text = data.decode('utf-8', errors='replace')
    for number, line in enumerate(text.splitlines(), 1):
        for label, pattern in patterns.items():
            for match in re.finditer(pattern, line):
                if not allowed(allowlist, path_for_rules, match.group(0)):
                    findings.append(f'{name}:{number}: {label}: {match.group(0)[:60]}')
        for match in EMAIL.finditer(line):
            value = match.group(0)
            if not SAFE_EMAIL.search(value) and not allowed(allowlist, path_for_rules, value):
                findings.append(f'{name}:{number}: email address: {value}')
        for match in UUID.finditer(line):
            if not allowed(allowlist, path_for_rules, match.group(0)):
                findings.append(f'{name}:{number}: UUID: {match.group(0)}')
        for match in HOME_PATH.finditer(line):
            if match.group(1) not in EXAMPLE_USERS and not allowed(allowlist, path_for_rules, match.group(0)):
                findings.append(f'{name}:{number}: a real home folder: {match.group(0)}')
    return findings


def scan_file(name):
    findings = []
    path = REPO / name
    parts = Path(name).parts
    if any(fnmatch.fnmatchcase(parts[-1], pattern) for pattern in FORBIDDEN_FILES) or FORBIDDEN_PARTS & set(parts):
        findings.append(f'{name}: looks like personal state, a credential or a build leftover')
    if path.is_symlink():
        target = os.readlink(path)
        if os.path.isabs(target) or not path.resolve().is_relative_to(REPO):
            findings.append(f'{name}: symlink pointing outside the repository ({target})')
        return findings, None
    if path.stat().st_size > MAX_BYTES:
        findings.append(f'{name}: larger than {MAX_BYTES:,} bytes')
    return findings, path.read_bytes()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--history', action='store_true')
    parser.add_argument('--patterns', type=Path, action='append', default=[])
    args = parser.parse_args()
    patterns = dict(SECRETS)
    patterns.update(machine_patterns())
    for file in args.patterns:
        for number, line in enumerate(file.read_text().splitlines(), 1):
            if line.strip() and not line.lstrip().startswith('#'):
                patterns[f'private pattern {file.name}:{number}'] = line.strip()
    allowlist = tomllib.loads((REPO / ALLOWLIST).read_text()).get('allow', [])
    findings = [f'manifest: {problem}' for problem in problems(load(REPO))]
    names = committable()
    for name in names:
        file_findings, data = scan_file(name)
        findings += file_findings
        if data is not None:
            findings += scan_text(name, name, data, patterns, allowlist)
    versions = 0
    if args.history:
        for label, name, data in history():
            versions += 1
            findings += scan_text(label, name, data, patterns, allowlist)
    for finding in findings:
        print(finding)
    summary = f'{len(names)} files' + (f' and {versions} historical file versions' if args.history else '')
    print(f'{summary} checked with {len(patterns)} patterns: ' + (f'{len(findings)} problem(s)' if findings else 'clean'))
    return 1 if findings else 0


if __name__ == '__main__':
    sys.exit(main())
