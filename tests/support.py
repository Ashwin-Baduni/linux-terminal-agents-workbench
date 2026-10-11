"""The staged home every suite tests: the repository's files, installed into a scratch folder.

Suites call home() at import. Under tests/run.py it returns the staged home the runner
made (HOME points at it). Run directly, it stages a fresh home, runs the same command
again inside it, and exits with that result. A suite never falls back to the code
installed in the real home.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from installer.stage import MARKER, stage  # noqa: E402


def environment(home):
    """The environment for a suite running inside a staged home."""
    env = {key: value for key, value in os.environ.items()
           if key not in ('XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME', 'XDG_CACHE_HOME')}
    env.update(HOME=str(home), WORKBENCH_STAGE=str(home), PYTHONDONTWRITEBYTECODE='1')
    return env


def home():
    staged = os.environ.get('WORKBENCH_STAGE')
    if not staged:
        scratch = Path(tempfile.mkdtemp(prefix='workbench-test-'))
        try:
            stage(scratch / 'home')
            code = subprocess.run(sys.orig_argv, env=environment(scratch / 'home')).returncode
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        sys.exit(code)
    staged = Path(staged)
    try:
        marker = json.loads((staged / MARKER).read_text())
    except (OSError, ValueError):
        sys.exit(f'{staged} is not a staged home; run the suites through tests/run.py.')
    if Path.home() != staged:
        sys.exit(f'HOME must be the staged home {staged}, not {Path.home()}.')
    if Path(marker['repository']) != REPO:
        sys.exit(f'{staged} was staged from {marker["repository"]}, not this checkout.')
    return staged
