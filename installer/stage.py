"""Lay the repository's files out in a scratch home, exactly where an installation puts them.

Staging writes files, seeds, links and folders only. It never merges into existing
settings, runs package managers, or touches the desktop, services or the real home;
the tests run against a staged home. Writes go through fsops.Home, so a symlinked
folder can never lead them outside the staged home.
"""
import json
import os
from pathlib import Path
import pwd
import re

from .fsops import Home
from .manifest import load
from .plan import link_target

MARKER = '.workbench-stage.json'
# Rendered paths go into desktop entries, tmux options and TOML strings unquoted.
SAFE_HOME = re.compile(r'/[A-Za-z0-9_./-]+')


def values_for(home):
    """Placeholder values for a target home, never taken from the machine running the stage."""
    home = Path(home)
    return {'HOME': str(home), 'CLAUDE_BIN': str(home / '.local/bin/claude'), 'GHOSTTY_BIN': '/usr/bin/ghostty'}


def stage(home, components=None, manifest=None, values=None):
    """Install the selected components' plain files into home; returns the target paths written."""
    manifest = manifest or load()
    home = Path(home).resolve()
    if not SAFE_HOME.fullmatch(str(home)):
        raise ValueError(f'{home}: use a home path without spaces or shell metacharacters.')
    real = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    if real.is_relative_to(home):
        raise ValueError('Staging writes into a scratch home, never the real one.')
    home.mkdir(parents=True, exist_ok=True)
    target = Home(home)
    values = values or values_for(home)
    written = []
    for entry in manifest.select(components):
        if entry.kind == 'merge':
            continue
        state = target.state(entry.target)
        if entry.kind == 'dir':
            target.mkdir(entry.target)
        elif entry.kind == 'link':
            target.symlink(entry.target, target.link_text(entry.target, link_target(entry.target, entry.link)))
        elif entry.kind == 'seed' and (state.kind != 'absent' or
                                       any(target.state(p).kind != 'absent' for p in entry.skip_if)):
            continue
        else:
            target.write(entry.target, manifest.render(entry, values), entry.mode)
        written.append(entry.target)
    chosen = manifest.closure(components or manifest.default)
    target.write(MARKER, (json.dumps({'repository': str(manifest.repo), 'components': chosen}, indent=2) + '\n').encode(),
                 0o644)
    return written
