"""Check that a real installation targets the tested desktop, before anything changes."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


def os_release(path='/etc/os-release'):
    values = {}
    try:
        for line in Path(path).read_text().splitlines():
            key, _, value = line.partition('=')
            values[key] = value.strip().strip('"')
    except OSError:
        pass
    return values


def output(*args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return ''
    return result.stdout.strip() if result.returncode == 0 else ''


def facts():
    """What the checks look at, gathered from this machine and session."""
    release = os_release()
    shell = output('gnome-shell', '--version') if shutil.which('gnome-shell') else ''
    return {
        'python': sys.version_info[:2],
        'uid': os.geteuid(),
        'os': release.get('ID', ''),
        'version': release.get('VERSION_ID', ''),
        'architecture': output('dpkg', '--print-architecture'),
        'desktop': os.environ.get('XDG_CURRENT_DESKTOP', ''),
        'gnome': (re.search(r'(\d+)(?:\.\d+)*', shell) or [None, ''])[1] if shell else '',
        'session': os.environ.get('XDG_SESSION_TYPE', ''),
        'systemd': bool(output('systemctl', '--user', 'show-environment')),
    }


def problems(supported, found):
    """Reasons this system is not the tested target; empty when it is."""
    reasons = []
    if found['python'] < tuple(int(x) for x in supported['python'].split('.')):
        reasons.append(f'Python {supported["python"]} or newer is needed.')
    if found['uid'] == 0:
        reasons.append('Run the installer as your own user, not with sudo; it asks for sudo only to install packages.')
    if found['os'] != supported['os'] or found['version'] != supported['version']:
        reasons.append(f'This is {found["os"] or "an unknown system"} {found["version"]}; '
                       f'the tested system is Ubuntu {supported["version"]}.')
    if found['architecture'] not in supported['architectures']:
        reasons.append(f'The processor architecture is {found["architecture"] or "unknown"}; '
                       'tested: ' + ', '.join(supported['architectures']) + '.')
    if supported['desktop'].upper() not in found['desktop'].upper().split(':'):
        reasons.append(f'The desktop is {found["desktop"] or "unknown"}; run the installer inside a GNOME session.')
    if found['gnome'] != supported['gnome']:
        reasons.append(f'GNOME Shell {found["gnome"] or "was not found"}; the tested version is {supported["gnome"]}.')
    if found['session'] != supported['session']:
        reasons.append(f'The session type is {found["session"] or "unknown"}; log in to the Wayland session.')
    if not found['systemd']:
        reasons.append('The systemd user manager is not reachable from this terminal.')
    return reasons
