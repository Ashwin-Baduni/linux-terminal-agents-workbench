#!/usr/bin/env python3
"""Check that the repository is safe to publish: no private names, paths, secrets or state.

  scripts/check-public.py                    files git would commit (tracked + new, not ignored)
  scripts/check-public.py --history          also every commit: its files, author, committer and message
  scripts/check-public.py --patterns FILE    extra private patterns, one regular expression per line

Patterns built from the machine running the check are always added: its home folder,
user name, host name, and git identity. Keep other private names (people, companies,
projects) in a patterns file outside the repository, so the check itself never
publishes them. Reviewed exceptions live in manifests/public-allowlist.toml.

Commit authors and committers must use a no-reply address. A name equal to the account
name in its own GitHub no-reply address is accepted, since that address publishes it.

This is a pattern scan. It catches the usual mistakes, not every possible secret.
"""
import argparse
import fnmatch
import os
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from installer.manifest import load, problems  # noqa: E402
import public_scan  # noqa: E402

# Files that are personal state, credentials or build leftovers, wherever they appear.
FORBIDDEN_FILES = ['*.pem', '*.key', 'id_rsa*', 'id_ed25519*', '.env', '.env.*', 'auth.json', '.credentials.json',
                   '.claude.json', 'models_cache.json', '*.sqlite', '*.sqlite3', '*.db', '*history*', '*.jsonl',
                   '*.pyc', '.ai-memory', 'settings.local.json', 'ultimate-*.md', '*.bak', '*.bak-*', '*~', '*.orig',
                   '*.swp', 'gschemas.compiled', 'projects.json', 'layout-metadata.json', 'journal.json']
FORBIDDEN_PARTS = {'__pycache__', 'node_modules', '.cache', 'resurrect', 'sessions', 'backups'}
MAX_BYTES = 1_000_000


def git(repo, *args, **kwargs):
    return subprocess.run(['git', '-C', str(repo), *args], capture_output=True, **kwargs)


def committable(repo):
    """The files the next commit would contain: tracked plus new files git does not ignore."""
    listing = git(repo, 'ls-files', '-z', '--cached', '--others', '--exclude-standard', text=True, check=True).stdout
    return sorted({name for name in listing.split('\0')
                   if name and ((repo / name).exists() or (repo / name).is_symlink())})


def history_files(repo):
    """(label, path, bytes) for every file version reachable from any ref."""
    seen = set()
    for commit in git(repo, 'rev-list', '--all', text=True, check=True).stdout.splitlines():
        tree = git(repo, 'ls-tree', '-rz', '--full-tree', commit, check=True).stdout
        for item in filter(None, tree.split(b'\0')):
            metadata, name = item.split(b'\t', 1)
            _, kind, sha = metadata.decode().split()
            name = os.fsdecode(name)
            if kind == 'blob' and (sha, name) not in seen:
                seen.add((sha, name))
                yield f'{name} (history {sha[:10]})', name, git(repo, 'cat-file', 'blob', sha, check=True).stdout


def history_commits(repo):
    """(sha, author name, author email, committer name, committer email, message) for every commit."""
    raw = git(repo, 'log', '--all', '--format=%H%x1f%an%x1f%ae%x1f%cn%x1f%ce%x1f%B%x1e', text=True).stdout
    for record in raw.split('\x1e'):
        fields = record.strip('\n').split('\x1f')
        if len(fields) == 6:
            yield fields


def forbidden_name(name):
    parts = Path(name).parts
    return any(fnmatch.fnmatchcase(parts[-1], pattern) for pattern in FORBIDDEN_FILES) or bool(FORBIDDEN_PARTS & set(parts))


def scan_file(repo, name):
    findings = []
    path = repo / name
    if forbidden_name(name):
        findings.append(f'{name}: looks like personal state, a credential or a build leftover')
    if path.is_symlink():
        target = os.readlink(path)
        if os.path.isabs(target) or not path.resolve().is_relative_to(repo):
            findings.append(f'{name}: symlink pointing outside the repository ({target})')
        return findings, None
    if path.stat().st_size > MAX_BYTES:
        findings.append(f'{name}: larger than {MAX_BYTES:,} bytes')
    return findings, path.read_bytes()


def check(repo, history=False, pattern_files=()):
    """(findings, summary) for a repository."""
    repo = Path(repo).resolve()
    found = public_scan.patterns(pattern_files)
    allowlist = public_scan.load_allowlist(repo)
    findings = []
    if (repo / 'manifests/files.toml').exists():
        findings += [f'manifest: {problem}' for problem in problems(load(repo))]
    names = committable(repo)
    for name in names:
        file_findings, data = scan_file(repo, name)
        findings += file_findings
        if data is not None:
            findings += public_scan.scan_text(name, name, data, found, allowlist)
    versions = commits = 0
    if history:
        flagged = set()
        for label, name, data in history_files(repo):
            versions += 1
            if forbidden_name(name) and name not in flagged:
                flagged.add(name)
                findings.append(f'{label}: looks like personal state, a credential or a build leftover')
            findings += public_scan.scan_text(label, name, data, found, allowlist)
        for sha, author, author_email, committer, committer_email, message in history_commits(repo):
            commits += 1
            findings += public_scan.scan_identity(f'commit {sha[:10]} author', author, author_email, found)
            findings += public_scan.scan_identity(f'commit {sha[:10]} committer', committer, committer_email, found)
            findings += public_scan.scan_text(f'commit {sha[:10]} message', '', message, found, allowlist)
    summary = f'{len(names)} files' + (f', {versions} file versions and {commits} commits' if history else '')
    return findings, f'{summary} checked with {len(found)} patterns'


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--history', action='store_true')
    parser.add_argument('--patterns', type=Path, action='append', default=[])
    parser.add_argument('--repo', type=Path, default=HERE.parent, help=argparse.SUPPRESS)
    args = parser.parse_args()
    findings, summary = check(args.repo, args.history, args.patterns)
    for finding in findings:
        print(finding)
    print(f'{summary}: ' + (f'{len(findings)} problem(s)' if findings else 'clean'))
    return 1 if findings else 0


if __name__ == '__main__':
    sys.exit(main())
