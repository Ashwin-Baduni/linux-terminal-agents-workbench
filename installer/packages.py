"""Ubuntu packages, the Yazi snap and the agent CLIs: only what is missing is installed.

Nothing already installed is upgraded. Package installation and agent sign-in are
outside the file rollback.
"""
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import urllib.parse
import urllib.request

LOCAL_HOSTS = ('127.0.0.1', 'localhost')
USER_AGENT = 'linux-terminal-agents-workbench-installer/1'


@dataclass
class Agent:
    name: str
    installer: str          # URL of the vendor's official install script
    interpreter: str
    args: list              # with {version}
    env: dict
    version: str            # requested release: a channel or an exact version
    pattern: str            # accepted values for version
    docs: str


def agents_from(component, versions):
    found = []
    for name, spec in component.get('cli', {}).items():
        version = versions.get(name) or spec['version']
        if not re.fullmatch(spec['versions'], version):
            raise ValueError(f'{name}: "{version}" is not a release this installer accepts ({spec["versions"]}).')
        found.append(Agent(name, spec['installer'], spec['interpreter'], spec['args'], spec.get('env', {}),
                           version, spec['versions'], spec['docs']))
    return found


def target_path(home, real):
    """PATH for programs run for the target user: its ~/.local/bin first."""
    base = os.environ.get('PATH', '/usr/bin:/bin') if real else '/usr/local/bin:/usr/bin:/bin'
    return str(Path(home) / '.local/bin') + ':' + base


def find(name, home, real):
    """The target user's executable: its ~/.local/bin, then (real installs only) its PATH."""
    return shutil.which(name, path=target_path(home, real) if real else str(Path(home) / '.local/bin'))


def version_of(binary, env=None):
    """Require a working CLI before reporting it as installed, including an existing executable."""
    remedy = f'Repair that CLI with its vendor installer, then run this installer again: {binary}'
    try:
        result = subprocess.run([binary, '--version'], capture_output=True, text=True, env=env, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f'{binary} --version could not finish ({error}). {remedy}') from error
    lines = (result.stdout.strip() or result.stderr.strip()).splitlines()
    if result.returncode or not lines:
        reason = f'exit {result.returncode}' if result.returncode else 'no version output'
        raise RuntimeError(f'{binary} --version failed ({reason}). {remedy}')
    return lines[0]


def download(url, folder):
    """Save an installer script to folder; returns (path, sha256). Only HTTPS (or a local test server)."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != 'https' and not (parts.scheme == 'http' and parts.hostname in LOCAL_HOSTS):
        raise ValueError(f'Refusing to download an installer over {parts.scheme}: {url}')
    # The vendors' download hosts refuse Python's default user agent (HTTP 403).
    request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read()
    if not data.startswith(b'#!'):
        raise ValueError(f'{url} did not return a shell script.')
    path = Path(folder) / (Path(parts.path).name or 'install.sh')
    path.write_bytes(data)
    path.chmod(0o600)
    return path, hashlib.sha256(data).hexdigest()


# How each vendor installer was reviewed to work (2026-10-11): the lines it must still contain
# for the arguments below to mean what we think, and where it states the releases it accepts.
CONTRACTS = {
    'claude': {
        'lines': ['TARGET="$1"', '"$binary_path" install ${TARGET:+"$TARGET"}'],
        'releases': re.compile(r'\[\[ ! "\$TARGET" =~ \^(.+?)\$ \]\]'),
        'named': (),
    },
    'codex': {
        'lines': ['--release)', 'NON_INTERACTIVE="${CODEX_NON_INTERACTIVE:-false}"',
                  'BIN_DIR="${CODEX_INSTALL_DIR:-$HOME/.local/bin}"', '*":$BIN_DIR:"*)', '"" | latest)'],
        'releases': re.compile(r"grep -Eq '\^(.+?)\$'"),
        'named': ('latest',),
    },
}


def verify_installer(agent, text):
    """Why the downloaded script cannot be run the reviewed way for agent's release; empty when it can."""
    contract = CONTRACTS.get(agent.name)
    if not contract:
        return [f'there is no reviewed contract for the {agent.name} installer']
    problems = [f'it no longer contains {line!r}' for line in contract['lines'] if line not in text]
    found = contract['releases'].search(text)
    if not found:
        problems.append('it no longer states which releases it accepts')
    elif agent.version not in contract['named']:
        accepted = found[1].replace('[:space:]', r'\s').replace('[:digit:]', r'\d')
        if not re.fullmatch(accepted, agent.version):
            problems.append(f'it does not accept the release "{agent.version}"')
    return problems


def install_agent(agent, home, downloads, log=print):
    """Run the vendor's own installer for one CLI, as the user; returns (path, version)."""
    folder = Path(tempfile.mkdtemp(prefix=agent.name + '-', dir=downloads))
    script, digest = download(agent.installer, folder)
    log(f'  {agent.name}: downloaded {agent.installer} to {script}')
    problems = verify_installer(agent, script.read_text(errors='replace'))
    if problems:
        raise RuntimeError(f'The {agent.name} installer changed since it was reviewed, so it was not run: '
                           + '; '.join(problems) + f'. It is kept at {script}; install {agent.name} by hand '
                           f'({agent.docs}) and run this installer again.')
    log(f'  {agent.name}: sha256 {digest}; running it for release "{agent.version}"')
    env = dict(os.environ, HOME=str(home), PATH=target_path(home, True), **agent.env)
    args = [arg.replace('{version}', agent.version) for arg in agent.args]
    result = subprocess.run([agent.interpreter, str(script), *args], env=env, cwd=folder, timeout=1800)
    if result.returncode:
        raise RuntimeError(f'The {agent.name} installer failed (exit {result.returncode}); see {script}.')
    binary = find(agent.name, home, True)
    if not binary:
        raise RuntimeError(f'The {agent.name} installer finished but {agent.name} is not in ~/.local/bin.')
    (folder / 'SHA256SUMS').write_text(f'{digest}  {script.name}\n')
    return binary, version_of(binary, env)


# --- Ubuntu packages and snaps --------------------------------------------------

def missing_apt(names):
    """Packages from names that are not installed."""
    missing = []
    for name in dict.fromkeys(names):
        result = subprocess.run(['dpkg-query', '-W', '-f=${Status}', name], capture_output=True, text=True)
        if result.returncode or 'install ok installed' not in result.stdout:
            missing.append(name)
    return missing


def unavailable_apt(names):
    """Missing packages apt has no candidate for (universe may be off, or the lists are stale)."""
    gone = []
    for name in names:
        policy = subprocess.run(['apt-cache', 'policy', name], capture_output=True, text=True).stdout
        if 'Candidate:' not in policy or 'Candidate: (none)' in policy:
            gone.append(name)
    return gone


def install_apt(names, log=print):
    if not names:
        return
    log('Installing Ubuntu packages (sudo asks for your password): ' + ' '.join(names))
    subprocess.run(['sudo', 'apt-get', 'update'], check=True)
    gone = unavailable_apt(names)
    if gone:
        raise RuntimeError('apt cannot find: ' + ', '.join(gone) + '. Enable the universe repository '
                           '(sudo add-apt-repository universe) and run the installer again.')
    subprocess.run(['sudo', 'apt-get', 'install', '-y', '--no-upgrade', *names], check=True)


def missing_snaps(snaps, home, real):
    missing = []
    for snap in snaps:
        listed = subprocess.run(['snap', 'list', snap['name']], capture_output=True).returncode == 0 \
            if shutil.which('snap') else False
        tools = ('yazi', 'ya') if snap['name'] == 'yazi' else (snap['name'],)
        if not listed and not all(find(tool, home, real) for tool in tools):
            missing.append(snap)
    return missing


def install_snaps(snaps, log=print):
    for snap in snaps:
        log(f'Installing the {snap["name"]} snap')
        subprocess.run(['sudo', 'snap', 'install', snap['name'], *(['--classic'] if snap.get('classic') else [])],
                       check=True)
