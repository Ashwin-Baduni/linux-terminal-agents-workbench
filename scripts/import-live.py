#!/usr/bin/env python3
"""Compare the repository with a machine that already runs the setup, and import chosen files.

Reads only the paths named in manifests/files.toml (their `live` path, default the
target). The live home path becomes @HOME@, then each entry's `sanitize` pairs
apply. Nothing outside the manifest is ever read.

  scripts/import-live.py                 report which files differ (the default)
  scripts/import-live.py --diff          also print each difference
  scripts/import-live.py --write PATH..  copy those repository files' live versions in
  scripts/import-live.py --write-all     copy every differing file in

Files changed in the repository on purpose (fixes, neutral names) show up as
differences until the live machine is reinstalled from the repository. Review
every difference; never write blindly.
"""
import argparse
import difflib
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from installer.manifest import load  # noqa: E402


def sanitized(entry, home):
    """The live file as the repository would hold it, or None when it is missing."""
    path = home / entry.live
    if not path.is_file():
        return None
    text = path.read_text()
    text = text.replace(str(home), '@HOME@')
    for old, new in entry.sanitize:
        text = text.replace(old, new)
    return text


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--home', type=Path, default=Path.home(), help='the home to read (default: yours)')
    parser.add_argument('--diff', action='store_true')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--write', nargs='+', metavar='PATH', default=[])
    group.add_argument('--write-all', action='store_true')
    args = parser.parse_args()
    manifest = load()
    home = args.home.resolve()
    sources = {}
    for entry in manifest.entries:
        if entry.source and entry.live:
            sources.setdefault(entry.source, []).append(entry)
    unknown = [path for path in args.write if path not in sources]
    if unknown:
        raise SystemExit('Not imported from a live file: ' + ', '.join(unknown))
    differing = problems = 0
    for source, entries in sorted(sources.items()):
        versions = {e.live: sanitized(e, home) for e in entries}
        present = {live: text for live, text in versions.items() if text is not None}
        if not present:
            print(f'missing   {source}  (no live file at ' + ', '.join('~/' + live for live in versions) + ')')
            continue
        if len(set(present.values())) > 1:
            print(f'conflict  {source}  (its live copies differ: ' + ', '.join(present) + ')')
            problems += 1
            continue
        text = next(iter(present.values()))
        entry = entries[0]
        if not entry.render and '@HOME@' in text:
            print(f'unsafe    {source}  (the live file names the home folder; add a sanitize pair or render it)')
            problems += 1
            continue
        current = manifest.repo / source
        old = current.read_text() if current.exists() else ''
        if old == text:
            continue
        differing += 1
        print(f'differs   {source}')
        if args.diff:
            sys.stdout.writelines(difflib.unified_diff(old.splitlines(True), text.splitlines(True),
                                                       'repository/' + source, 'live/' + entry.live))
        if args.write_all or source in args.write:
            current.parent.mkdir(parents=True, exist_ok=True)
            current.write_text(text)
            print(f'  written from ~/{entry.live}')
    print(f'{differing} file(s) differ' + (f'; {problems} need attention' if problems else ''))
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
