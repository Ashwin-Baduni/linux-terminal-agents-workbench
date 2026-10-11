#!/usr/bin/env python3
"""Run the test suites against this checkout, installed into a scratch home.

  tests/run.py               every suite
  tests/run.py --quick       every suite except the relay (which takes several minutes)
  tests/run.py memory repo   only the named suites
  tests/run.py --keep        leave the scratch folder for inspection

The runner stages the default components into a new folder under the system temporary
directory and runs each suite with HOME pointing there, so nothing reads or changes
the real home. The suites need tmux, git, bash and Python 3.11+; the workspace check
also needs Yazi.
"""
import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import support  # noqa: E402

SUITES = {
    'repo': ['repo/test_repo.py', 'repo/test_publication.py'],
    'installer': ['installer/test_installer.py'],
    'memory': ['memory/test_ai_memory.py'],
    'copy': ['workspace/test_copy_everything.py'],
    'workspace': ['workspace/check_workspace.py'],
    'relay': ['agents/test_offline.py'],
}
SLOW = {'relay'}


def main():
    if sys.version_info < (3, 11):
        sys.exit('The tests need Python 3.11 or newer.')
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('suites', nargs='*', metavar='SUITE', help='any of: ' + ', '.join(SUITES))
    parser.add_argument('--quick', action='store_true', help='skip the slow relay suite')
    parser.add_argument('--keep', action='store_true', help='keep the scratch folder')
    args = parser.parse_args()
    unknown = [name for name in args.suites if name not in SUITES]
    if unknown:
        parser.error('unknown suite(s): ' + ', '.join(unknown))
    chosen = args.suites or [name for name in SUITES if not (args.quick and name in SLOW)]
    scratch = Path(tempfile.mkdtemp(prefix='workbench-tests-'))
    home = scratch / 'home'
    results = []
    try:
        written = support.stage(home)
        print(f'Staged {len(written)} files into {home}', flush=True)
        env = support.environment(home)
        for name in chosen:
            for script in SUITES[name]:
                print(f'\n=== {name}: {script}', flush=True)
                started = time.monotonic()
                code = subprocess.run([sys.executable, str(HERE / script)], cwd=scratch, env=env).returncode
                results.append((name, script, code, time.monotonic() - started))
    finally:
        if args.keep:
            print(f'\nScratch folder kept: {scratch}')
        else:
            shutil.rmtree(scratch, ignore_errors=True)
    print('\nSummary')
    for name, script, code, seconds in results:
        print(f'  {"PASS" if code == 0 else "FAIL"}  {name:<10} {script}  ({seconds:.1f} s)')
    return 1 if any(code for *_, code, _ in results) else 0


if __name__ == '__main__':
    sys.exit(main())
