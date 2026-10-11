"""Patterns and checks shared by check-public.py and import-live.py.

Patterns built from the machine running a check are always included: its home folder,
user name, host name and global git identity. Other private names come from a patterns
file kept outside the repository (one regular expression per line, # for comments), so
the check itself never publishes them.
"""
import fnmatch
import os
from pathlib import Path
import pwd
import re
import socket
import subprocess
import tomllib

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
# Addresses meant to be public: examples, local test identities and no-reply addresses.
SAFE_EMAIL = re.compile(r'@(?:example\.(?:com|org|net)|localhost)$|^noreply@|@users\.noreply\.github\.com$')
GITHUB_NOREPLY = re.compile(r'^(?:\d+\+)?(?P<user>[A-Za-z0-9-]+)@users\.noreply\.github\.com$')
UUID = re.compile(r'(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b')
HOME_PATH = re.compile(r'/home/([A-Za-z0-9_.-]+)')
EXAMPLE_USERS = {'x', 'user', 'example', 'someone', 'you'}
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


def patterns(files=()):
    found = dict(SECRETS)
    found.update(machine_patterns())
    for file in files:
        for number, line in enumerate(Path(file).read_text().splitlines(), 1):
            if line.strip() and not line.lstrip().startswith('#'):
                try:
                    re.compile(line.strip())
                except re.error as error:
                    raise SystemExit(f'{file}:{number}: not a regular expression ({error}): {line.strip()}')
                found[f'private pattern {Path(file).name}:{number}'] = line.strip()
    return found


def load_allowlist(repo):
    path = Path(repo) / ALLOWLIST
    return tomllib.loads(path.read_text()).get('allow', []) if path.exists() else []


def allowed(allowlist, name, value, line=''):
    """True when a reviewed entry allows value in this file (and, if it names one, on a line with its context)."""
    return any((fnmatch.fnmatchcase(name, item['path']) or name == ALLOWLIST) and item['value'] == value
               and (name == ALLOWLIST or item.get('context', '') in line) for item in allowlist)


def scan_text(label, name, data, found_patterns, allowlist):
    """Findings for one text: secrets, private names, emails, UUIDs and real home folders."""
    findings = []
    if isinstance(data, bytes):
        if b'\0' in data[:8192]:
            return findings
        data = data.decode('utf-8', errors='replace')
    for number, line in enumerate(data.splitlines(), 1):
        for kind, pattern in found_patterns.items():
            for match in re.finditer(pattern, line):
                if not allowed(allowlist, name, match.group(0), line):
                    findings.append(f'{label}:{number}: {kind}: {match.group(0)[:60]}')
        for match in EMAIL.finditer(line):
            value = match.group(0)
            if not SAFE_EMAIL.search(value) and not allowed(allowlist, name, value, line):
                findings.append(f'{label}:{number}: email address: {value}')
        for match in UUID.finditer(line):
            if not allowed(allowlist, name, match.group(0), line):
                findings.append(f'{label}:{number}: UUID: {match.group(0)}')
        for match in HOME_PATH.finditer(line):
            if match.group(1) not in EXAMPLE_USERS and not allowed(allowlist, name, match.group(0), line):
                findings.append(f'{label}:{number}: a real home folder: {match.group(0)}')
    return findings


def scan_identity(label, name, email, found_patterns):
    """Findings for a commit's author or committer."""
    findings = []
    if not SAFE_EMAIL.search(email):
        findings.append(f'{label}: email address {email} (use a no-reply address before publishing)')
    noreply = GITHUB_NOREPLY.match(email)
    if noreply and name == noreply['user']:
        return findings     # The public account name that the no-reply address itself publishes.
    for kind, pattern in found_patterns.items():
        if re.search(pattern, name):
            findings.append(f'{label}: name "{name}" matches {kind}')
    return findings
