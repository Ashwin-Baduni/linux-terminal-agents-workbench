"""Lay the repository's files out in a scratch home, exactly where an installation puts them.

Staging writes files, seeds, links and folders only. It never merges into existing
settings, runs package managers, or touches the desktop, services or the real home;
the tests run against a staged home, and the installer builds on the same plan.
"""
import json
import os
from pathlib import Path
import pwd
import re
import tempfile

from .manifest import load

MARKER = '.workbench-stage.json'
# Rendered paths go into desktop entries, tmux options and TOML strings unquoted.
SAFE_HOME = re.compile(r'/[A-Za-z0-9_./-]+')


def values_for(home):
    """Placeholder values for a target home, never taken from the machine running the stage."""
    home = Path(home)
    return {'HOME': str(home), 'CLAUDE_BIN': str(home / '.local/bin/claude')}


def write(path, data, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name + '.')
    with os.fdopen(handle, 'wb') as stream:
        stream.write(data)
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def stage(home, components=None, manifest=None, values=None):
    """Install the selected components' plain files into home; returns the target paths written."""
    manifest = manifest or load()
    home = Path(home).resolve()
    if not SAFE_HOME.fullmatch(str(home)):
        raise ValueError(f'{home}: use a home path without spaces or shell metacharacters.')
    real = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    if real.is_relative_to(home):
        raise ValueError('Staging writes into a scratch home, never the real one.')
    values = values or values_for(home)
    written = []
    for entry in manifest.select(components):
        path = home / entry.target
        if entry.kind == 'merge':
            continue
        if entry.kind == 'dir':
            path.mkdir(parents=True, exist_ok=True)
        elif entry.kind == 'link':
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.is_symlink() or path.exists():
                path.unlink()
            path.symlink_to(entry.link)
        elif entry.kind == 'seed' and (path.exists() or path.is_symlink()):
            continue
        else:
            write(path, manifest.render(entry, values), entry.mode)
        written.append(entry.target)
    chosen = manifest.closure(components or manifest.default)
    (home / MARKER).write_text(json.dumps({'repository': str(manifest.repo), 'components': chosen}, indent=2) + '\n')
    return written
