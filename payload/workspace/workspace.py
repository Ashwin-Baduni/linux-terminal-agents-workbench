#!/usr/bin/env python3
"""Mouse-driven Ghostty/tmux workspace controls; no user paths run through a shell."""
from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import uuid

HOME = Path.home()
SELF = HOME / '.local/bin/workspace-control'
CONFIG = HOME / '.config/tmux/workspace.conf'
STATE = Path(os.environ.get('WORKSPACE_STATE_DIR', HOME / '.local/state/workspace'))
SOCKET = os.environ.get('WORKSPACE_TMUX_SOCKET', 'workspace')
RESURRECT = HOME / '.local/share/workspace/tmux-resurrect/scripts'
# Yazi's snap puts ya in /snap/bin, which a desktop session's PATH may lack.
YA = '/snap/bin/ya' if os.path.exists('/snap/bin/ya') else shutil.which('ya') or 'ya'
SEP = '\x1f'


def run(args, *, check=True, **kwargs):
    return subprocess.run([str(arg) for arg in args], check=check, text=True,
                          capture_output=True, **kwargs)


def spawn(args, **kwargs):
    return subprocess.Popen([str(arg) for arg in args], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True, **kwargs)


def tmux(*args, check=True, **kwargs):
    selector = ['-S', SOCKET] if '/' in SOCKET else ['-L', SOCKET]
    return run(['tmux', *selector, *args], check=check, **kwargs)


@contextlib.contextmanager
def server(name):
    """Point tmux() at another server, such as an ordinary tab's private agent server."""
    global SOCKET
    previous, SOCKET = SOCKET, name
    try:
        yield
    finally:
        SOCKET = previous


def private_sockets():
    """Sockets of agent-terminal's one-pane servers for agents in ordinary Ghostty tabs."""
    runtime = os.environ.get('XDG_RUNTIME_DIR')
    return sorted(str(p) for p in Path(runtime).glob('agent-terminal-*/tmux.sock')) if runtime else []


def private_agent(pid):
    """agent-terminal panes run `python3 agent-terminal --child STATUS BINARY ...`."""
    try:
        args = Path('/proc', pid, 'cmdline').read_bytes().split(b'\0')
    except OSError:
        return ''
    return Path(os.fsdecode(args[4])).name if len(args) > 4 and args[2] == b'--child' else ''


def clean(text):
    return re.sub(r'[\x00-\x1f\x7f]', ' ', str(text))


def dialog(kind, title, text='', *extra):
    result = run(['zenity', '--' + kind, '--title=' + title,
                  '--text=' + clean(text), *extra], check=False)
    return result.stdout.rstrip('\n') if result.returncode == 0 else None


def choose(title, rows, columns=('Action',), extra=(), ok_label='Open'):
    args = ['zenity', '--list', '--title=' + title, '--width=850', '--height=470',
            '--column=ID', *['--column=' + c for c in columns],
            '--hide-column=1', '--print-column=1', '--ok-label=' + ok_label, *extra]
    for row in rows:
        args.extend(clean(value) for value in row)
    result = run(args, check=False)
    # Zenity also prints the label of a clicked extra button.
    return result.stdout.rstrip('\n') or None


def info(text):
    dialog('info', 'Workspace', text)


def error(text):
    dialog('error', 'Workspace', text)


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        temp = Path(file.name)
    os.chmod(temp, 0o600)
    temp.replace(path)


def catalog(update=None):
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / 'catalog.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = STATE / 'projects.json'
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = {'projects': []}
        if update:
            update(data)
            atomic_json(path, data)
        return data


def remember(folder, favourite=None):
    folder = str(Path(folder).resolve())
    def update(data):
        existing = next((p for p in data['projects'] if p['path'] == folder), None)
        if existing is None:
            existing = {'path': folder, 'favourite': False}
            data['projects'].append(existing)
        existing['last_opened'] = time.time()
        if favourite is not None:
            existing['favourite'] = favourite
        data['projects'] = sorted(data['projects'],
                                  key=lambda p: (not p['favourite'], -p.get('last_opened', 0)))[:60]
    catalog(update)


def records(command, fields, *args):
    result = tmux(command, *args, '-F', SEP.join('#{' + f + '}' for f in fields), check=False)
    if result.returncode:
        return []
    return [dict(zip(fields, line.split(SEP))) for line in result.stdout.splitlines()]


def sessions():
    return records('list-sessions', ['session_id', 'session_name', 'session_path', 'session_attached',
                                    '@workspace_label'])


def workspace_label(session):
    return session.get('@workspace_label') or 'Workspace ' + str(int(session['session_id'][1:]) + 1)


def panes(target=None, *, all_windows=False):
    args = ['-t', target] if target else ['-a']
    if target and all_windows:
        args.insert(0, '-s')
    return records('list-panes', ['pane_id', 'pane_pid', 'pane_current_path', 'pane_current_command',
                                'pane_active', 'pane_dead', 'pane_width', 'pane_height', 'pane_title',
                                '@name', '@workspace_status', '@workspace_agent', '@workspace_thread',
                                '@workspace_transcript', '@workspace_started'], *args)


def pane(identifier):
    result = tmux('display-message', '-p', '-t', identifier,
                  SEP.join('#{' + f + '}' for f in ('pane_id', 'session_id', 'session_path',
                                                   'pane_current_path', 'window_id')), check=False)
    if result.returncode:
        raise ValueError('This workspace pane has closed.')
    return dict(zip(('id', 'session', 'root', 'cwd', 'window'), result.stdout.rstrip('\n').split(SEP)))


def option(target, name, value):
    tmux('set-option', '-p', '-t', target, name, str(value), check=False)


def new_pane(target, name, command=None, folder=None, *, vertical=False, percent=None):
    source = pane(target)
    args = ['split-window', '-P', '-F', '#{pane_id}', '-t', source['id'],
            '-v' if vertical else '-h', '-c', folder or source['cwd']]
    if percent:
        args += ['-l', str(percent) + '%']
    if command:
        args += [shlex.join([str(a) for a in command])]
    identifier = tmux(*args).stdout.strip()
    option(identifier, '@name', name)
    if name != 'Directory':
        option(identifier, 'allow-passthrough', 'on')
    return identifier


def pick_folder(title='Choose a workspace folder'):
    result = run(['zenity', '--file-selection', '--directory', '--title=' + title], check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def workspace_rows(live, projects):
    favourites = {p['path']: p.get('favourite', False) for p in projects}
    selections, rows = {}, []
    for session in live:
        folder = session['session_path']
        identifier = 'session:' + session['session_id']
        selections[identifier] = {'folder': folder, 'session': session['session_id']}
        rows.append((identifier, '★' if favourites.get(folder) else '', Path(folder).name,
                     workspace_label(session), 'Open' if int(session['session_attached']) else 'Parked', folder))
    opened = {s['session_path'] for s in live}
    for project in projects:
        folder = project['path']
        if folder not in opened and Path(folder).is_dir():
            identifier = 'project:' + folder
            selections[identifier] = {'folder': folder}
            rows.append((identifier, '★' if project.get('favourite') else '', Path(folder).name,
                         '—', 'Closed', folder))
    rows.sort(key=lambda row: (row[1] != '★', row[4] == 'Closed', row[2], row[3]))
    return rows, selections


def close_workspace_picker():
    live = sessions()
    if not live:
        info('No running or parked workspaces to close.')
        return
    rows = [(s['session_id'], workspace_label(s), Path(s['session_path']).name,
             'Open' if int(s['session_attached']) else 'Parked', s['session_path']) for s in live]
    selected = choose('Close a workspace — stop its agents', rows,
                      ('Workspace', 'Project', 'State', 'Folder'), ok_label='Close workspace')
    session = next((s for s in live if s['session_id'] == selected), None)
    if session and dialog('question', 'Close workspace',
                          'Stop all terminals and agents in ' + workspace_label(session) +
                          '? Folder: ' + session['session_path']) is not None:
        tmux('kill-session', '-t', session['session_id'], check=False)


def picker():
    while True:
        live = sessions()
        projects = catalog()['projects']
        for path in {s['session_path'] for s in live}:
            if not any(p['path'] == path for p in projects):
                projects.append({'path': path, 'favourite': False, 'last_opened': 0})
        projects.sort(key=lambda p: (not p['favourite'], -p.get('last_opened', 0)))
        rows, selections = workspace_rows(live, projects)
        result = choose('Workspaces — X/Quit stops agents; Park keeps them running', rows,
                        ('Favourite', 'Project', 'Workspace', 'State', 'Folder'),
                        ('--extra-button=New workspace…', '--extra-button=Close workspace…',
                         '--extra-button=More…'))
        if result == 'Close workspace…':
            close_workspace_picker()
            continue
        if result == 'New workspace…':
            folder = choose('New independent workspace',
                            [(p['path'], Path(p['path']).name, p['path']) for p in projects if Path(p['path']).is_dir()],
                            ('Project', 'Folder'), ('--extra-button=Choose folder…',), ok_label='Create workspace')
            if folder == 'Choose folder…':
                folder = pick_folder()
            if folder:
                return {'folder': folder, 'new': True}
            continue
        if result == 'More…':
            result = choose('Workspace options', [('folder', 'Choose a project folder'),
                                                  ('favourites', 'Manage favourites'),
                                                  ('restore', 'Restore saved layouts')])
            if result == 'folder':
                folder = pick_folder()
                if folder:
                    return {'folder': folder}
                continue
            if not result:
                continue
        if result == 'favourites':
            chosen = choose('Toggle a favourite', [(p['path'], '★ ' + p['path'] if p['favourite'] else p['path'])
                                                   for p in projects], ok_label='Toggle favourite')
            if chosen:
                old = next(p for p in projects if p['path'] == chosen)
                remember(chosen, not old['favourite'])
            continue
        if result == 'restore':
            restore()
            continue
        return selections.get(result)


def ensure_session(folder, new=False):
    folder = str(Path(folder).resolve(strict=True))
    if not Path(folder).is_dir() or any(c in folder for c in '\n\r\x1f'):
        raise ValueError('Choose a folder with a single-line path.')
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / 'open.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        existing = None if new else next((s for s in sessions() if s['session_path'] == folder), None)
        if existing:
            identifier = existing['session_id']
        else:
            name = 'work-' + hashlib.sha256(folder.encode()).hexdigest()[:12]
            if any(s['session_name'] == name for s in sessions()):
                name += '-' + uuid.uuid4().hex[:8]
            identifier = tmux('-f', CONFIG, 'new-session', '-d', '-P', '-F', '#{session_id}',
                              '-s', name, '-c', folder, '-x', '160', '-y', '44').stdout.strip()
            first = panes(identifier)[0]['pane_id']
            option(first, '@name', 'Agent 1')
            option(first, 'allow-passthrough', 'on')
            new_pane(first, 'Agent 2', folder=folder)
            # Directory opens on demand from the right-click menu (Show Directory), not at start.
            tmux('select-pane', '-t', first)
            tmux('set-option', '-t', identifier, 'destroy-unattached', 'off')
        tmux('set-option', '-t', identifier, '@workspace_path', folder)
        current = next(s for s in sessions() if s['session_id'] == identifier)
        tmux('set-option', '-t', identifier, '@workspace_label', workspace_label(current))
        remember(folder)
    return identifier


def workspace_args(selection):
    folder = selection['folder']
    args = ['xdg-terminal-exec', '--dir=' + folder, str(SELF), 'open', folder]
    if selection.get('new'):
        args.append('--new')
    elif selection.get('session'):
        args += ['--session', selection['session']]
    return args


def open_workspace(folder=None, pick=False, new=False, session=None):
    if pick:
        selected = picker()
        if not selected:
            return
        folder, session = selected['folder'], selected.get('session')
        new = new or selected.get('new', False)
    folder = str(Path(folder or os.getcwd()).resolve())
    if not Path(folder).is_dir():
        raise ValueError('Not a folder: ' + folder)
    inside = os.environ.get('TMUX', '')
    expected_socket = tmux('display-message', '-p', '#{socket_path}', check=False).stdout.strip() if inside else ''
    foreign_tmux = inside and inside.split(',')[0] != expected_socket
    if not sys.stdout.isatty() or (new and inside) or foreign_tmux:
        args = workspace_args({'folder': folder, 'new': new, 'session': session})
        os.environ.pop('TMUX', None)
        os.environ.pop('TMUX_PANE', None)
        os.execvp(args[0], args)
    if session:
        selected = next((s for s in sessions() if s['session_id'] == session), None)
        if not selected or selected['session_path'] != folder:
            raise ValueError('That workspace has closed. Choose the project again from Workspace.')
        target = session
    else:
        target = ensure_session(folder, new=new)
    if os.environ.get('TMUX'):
        tmux('switch-client', '-t', target)
    else:
        selector = ['-S', SOCKET] if '/' in SOCKET else ['-L', SOCKET]
        os.execvp('tmux', ['tmux', *selector, 'attach-session', '-d', '-t', target])


class AppServer:
    """Read-only JSON-RPC requests over the local Codex Unix WebSocket."""
    def __init__(self):
        candidates = [Path(os.environ['CODEX_APP_SERVER_SOCKET'])] if os.environ.get('CODEX_APP_SERVER_SOCKET') else []
        runtime = Path(os.environ.get('XDG_RUNTIME_DIR', '/run/user/' + str(os.getuid())))
        candidates += list(runtime.glob('codex/**/app-server.sock'))
        candidates += [p for p in Path('/tmp').glob(f'codex-daemon-{os.getuid()}/*') if p.is_socket()]
        if not candidates:
            raise OSError('Codex app server is not running.')
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(3)
        self.sock.connect(str(candidates[0]))
        self.buffer = b''
        self.counter = 0
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(('GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                          f'Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n').encode())
        while b'\r\n\r\n' not in self.buffer:
            self.buffer += self.sock.recv(4096)
        header, self.buffer = self.buffer.split(b'\r\n\r\n', 1)
        if not header.startswith(b'HTTP/1.1 101'):
            self.close()
            raise OSError('Codex local connection was rejected.')
        self.request('initialize', {'clientInfo': {'name': 'workbench_workspace', 'title': 'Workspace', 'version': '1.0'},
                                    'capabilities': {'experimentalApi': True}})
        self.send({'method': 'initialized', 'params': {}})

    def close(self):
        self.sock.close()

    def read(self, count):
        while len(self.buffer) < count:
            chunk = self.sock.recv(max(4096, count - len(self.buffer)))
            if not chunk:
                raise OSError('Codex disconnected.')
            self.buffer += chunk
        result, self.buffer = self.buffer[:count], self.buffer[count:]
        return result

    def frame(self, data, opcode=1):
        mask = os.urandom(4)
        length = len(data)
        header = bytes([0x80 | opcode, 0x80 | (length if length < 126 else 126 if length < 65536 else 127)])
        if length >= 126:
            header += struct.pack('!H' if length < 65536 else '!Q', length)
        self.sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def send(self, value):
        self.frame(json.dumps(value).encode())

    def receive(self):
        fragments = b''
        while True:
            first, second = self.read(2)
            count = second & 127
            if count in (126, 127):
                count = struct.unpack('!H' if count == 126 else '!Q', self.read(2 if count == 126 else 8))[0]
            if count > 32 * 1024 * 1024:
                raise OSError('Codex response is too large.')
            mask = self.read(4) if second & 128 else None
            data = self.read(count)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            opcode = first & 15
            if opcode == 8:
                raise OSError('Codex closed the connection.')
            if opcode == 9:
                self.frame(data, 10)
                continue
            if opcode == 10:
                continue
            fragments += data
            if first & 128:
                return json.loads(fragments)

    def request(self, method, params):
        if method not in ('initialize', 'thread/list', 'thread/turns/list'):
            raise ValueError('Workspace only makes read-only Codex requests.')
        self.counter += 1
        identifier = self.counter
        self.send({'id': identifier, 'method': method, 'params': params})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = self.receive()
            if result.get('id') == identifier:
                if 'error' in result:
                    raise OSError(str(result['error']))
                return result['result']
        raise OSError('Codex request timed out.')


def status_for_thread(thread, previous=''):
    status = thread.get('status', {})
    kind = status.get('type')
    if kind == 'active':
        return 'Needs input' if set(status.get('activeFlags', [])) & {'waitingOnApproval', 'waitingOnUserInput'} else 'Working'
    if kind == 'systemError':
        return 'Error'
    if kind == 'idle':
        return 'Finished' if previous in ('Working', 'Needs input', 'Finished') else 'Ready'
    return 'Ready'


def codex_title_name(title):
    title = re.sub(r'^[\u2800-\u28ff✓✔✗⚠⏸▶●◐◑◒◓ ]+', '', title.strip())
    # Attention titles add a separate segment ahead of the conversation name.
    title = re.sub(r'^\[[^\]]*\]\s*(?:Action Required|Working|Idle|Done|Finished)\s*\|\s*',
                   '', title, flags=re.I)
    # The last segment is the project, so a conversation name may contain |.
    return title.rsplit(' | ', 1)[0]


def notify(target, status):
    data = pane(target)
    title = tmux('display-message', '-p', '-t', target, '#{@name}').stdout.strip() or 'Agent'
    spawn(['notify-send', '-a', 'Workspace', '-i', 'utilities-terminal', '-t', '8000',
           title + ' — ' + status, Path(data['root']).name])


def set_status(target, status, *, attention=False):
    previous = tmux('show-option', '-p', '-v', '-t', target, '@workspace_status', check=False).stdout.strip()
    if previous == status:
        return
    option(target, '@workspace_status', status)
    refresh_titles()
    if attention and status in ('Finished', 'Needs input', 'Error') and previous in ('Working', 'Needs input'):
        notify(target, status)


# Panes showing tools rather than an agent or terminal stay out of the window title.
TOOL_PANES = {'Directory', 'Preview', 'Logs', 'Changes', 'History', 'GPU', 'Relay'}
# Window titles cannot be coloured, so each state gets a coloured dot in the pane labels' colours.
TITLE_STATES = {'Working': '🔵 Working', 'Starting': '🔵 Starting', 'Finished': '🟢 Finished',
                'Needs input': '🔴 Needs you', 'Error': '🔴 Error', 'Ready': '⚪ Ready', 'Running': '⚪ Running',
                'Stopped': '⚪ Stopped'}


def home_path(path):
    home = str(HOME)
    return '~' + path[len(home):] if path == home or path.startswith(home + '/') else path


def refresh_titles():
    """Title each window: workspace label (or agent), folder, then each pane's state left to right."""
    rows = records('list-panes', ['session_id', 'session_path', 'window_name', '@workspace_label',
                                  '@workspace_title', '@name', '@workspace_agent', '@workspace_status'], '-a')
    for identifier in dict.fromkeys(r['session_id'] for r in rows):
        own = [r for r in rows if r['session_id'] == identifier]
        states = [TITLE_STATES.get(r['@workspace_status'], r['@workspace_status']) for r in own
                  if r['@workspace_status'] and r['@name'] not in TOOL_PANES and r['window_name'] != 'Resources']
        name = own[0]['@workspace_label'] or own[0]['@workspace_agent'].title() or 'Workspace'
        title = clean(' — '.join(filter(None, (name, home_path(own[0]['session_path']), ' · '.join(states)))))
        if title != own[0]['@workspace_title']:
            tmux('set-option', '-t', identifier, '@workspace_title', title, check=False)


def runs_codex(p):
    return p['pane_current_command'] == 'codex' or p['private'] and p['@workspace_agent'] == 'codex'


def monitor_once(api=None):
    # The workspace server first, then each ordinary tab's private agent server.
    servers = [(SOCKET, False), *((s, True) for s in private_sockets())]
    current = []
    for socket_name, private in servers:
        with server(socket_name):
            for p in panes():
                p['socket'], p['private'] = socket_name, private
                if private and not p['@workspace_agent'] and private_agent(p['pane_pid']):
                    p['@workspace_agent'] = private_agent(p['pane_pid'])
                    option(p['pane_id'], '@workspace_agent', p['@workspace_agent'])
                current.append(p)
    codex = [p for p in current if runs_codex(p)]
    threads = []
    if codex and api:
        folders = list({p['pane_current_path'] for p in codex})
        threads = api.request('thread/list', {'cwd': folders, 'limit': 100, 'sortKey': 'updated_at',
                                               'useStateDbOnly': True, 'sourceKinds': ['cli', 'vscode', 'appServer']})['data']
    for p in current:
        with server(p['socket']):
            monitor_pane(p, threads)
    for socket_name, _ in servers:
        with server(socket_name):
            refresh_titles()  # Also drops panes that closed since the last poll.


def monitor_pane(p, threads):
    name, cmd, identifier = p['@name'], p['pane_current_command'], p['pane_id']
    if name == 'Directory':
        return
    if p['pane_dead'] == '1':
        set_status(identifier, 'Stopped')
    elif runs_codex(p):
        option(identifier, '@workspace_agent', 'codex')
        title = codex_title_name(p['pane_title'])
        local_threads = [t for t in threads if t.get('cwd') == p['pane_current_path']]
        matches = [t for t in local_threads if t.get('name') == title]
        cached = next((t for t in local_threads if t['id'] == p['@workspace_thread']), None)
        if len(matches) > 1 and cached in matches:
            matches = [cached]
        elif not matches and cached and (not title or not cached.get('name')):
            matches = [cached]
        if not matches:
            option(identifier, '@workspace_thread', '')
        if len(matches) == 1:
            thread = matches[0]
            option(identifier, '@workspace_thread', thread['id'])
            set_status(identifier, status_for_thread(thread, p['@workspace_status']), attention=not p['private'])
        elif p['pane_title'] and '\u2800' <= p['pane_title'][0] <= '\u28ff':
            set_status(identifier, 'Working')
        else:
            set_status(identifier, 'Running')
    elif cmd in ('bash', 'zsh', 'fish'):
        if p['@workspace_started']:
            set_status(identifier, 'Working')
        elif p['@workspace_status'] not in ('Finished', 'Error'):
            set_status(identifier, 'Ready')
    elif cmd == 'claude' or p['@workspace_agent'] == 'claude':
        if not p['@workspace_status']:
            set_status(identifier, 'Running')
    elif name.startswith('Agent'):
        set_status(identifier, 'Working' if p['@workspace_started'] else 'Running')


def monitor():
    STATE.mkdir(parents=True, exist_ok=True)
    lock = (STATE / 'monitor.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    api = None
    while True:
        try:
            if api is None:
                api = AppServer()
            monitor_once(api)
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            if api:
                api.close()
            api = None
            try:
                monitor_once()
            except (OSError, ValueError, KeyError, subprocess.SubprocessError):
                pass  # A pane may disappear while the monitor is polling it.
        time.sleep(3)


def hook():
    """Claude events update only the matching workspace or private-tab pane; never alter prompts."""
    try:
        event = json.load(sys.stdin)
        target = os.environ.get('TMUX_PANE')
        if not target or not os.environ.get('TMUX'):
            return
        socket_name = os.environ['TMUX'].split(',')[0]
        expected = tmux('display-message', '-p', '#{socket_path}', check=False).stdout.strip()
        private = socket_name != expected
        if private and socket_name not in private_sockets():
            return
        with server(socket_name if private else SOCKET):
            option(target, '@workspace_agent', 'claude')
            if event.get('transcript_path'):
                option(target, '@workspace_transcript', event['transcript_path'])
            name = event.get('hook_event_name')
            mapping = {'SessionStart': 'Ready', 'UserPromptSubmit': 'Working', 'PreToolUse': 'Working',
                       'PermissionRequest': 'Needs input', 'Stop': 'Finished', 'StopFailure': 'Error', 'SessionEnd': 'Ready'}
            state = mapping.get(name)
            if name == 'Notification' and event.get('notification_type') in ('permission_prompt', 'elicitation_dialog'):
                state = 'Needs input'
            if state:
                # Ordinary tabs keep their previous behavior: no desktop notifications.
                set_status(target, state, attention=not private)
            forward_to_pair(target, event)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass


PAIR_EVENTS = ('UserPromptSubmit', 'Stop', 'StopFailure', 'SessionStart', 'SessionEnd')


def forward_to_pair(target, event):
    """A Claude that joined an agent pair after it started reports its turns to the pair's relay:
    turn start and end, a failed turn, a cleared or compacted conversation, quitting, and each tool
    call as a heartbeat (a file the relay reads, not an event)."""
    name = event.get('hook_event_name')
    if os.environ.get('AGENT_PAIR_DIR') or name not in PAIR_EVENTS + ('PreToolUse',):
        return  # Agents launched by agent-pair report through their own hook.
    values = tmux('display-message', '-p', '-t', target, '#{@pair_dir}' + SEP + '#{@pair_role}',
                  check=False).stdout.rstrip('\n').split(SEP)
    if len(values) != 2 or values[1] not in ('orchestrator', 'coder') or not Path(values[0], 'events').is_dir():
        return
    if name == 'PreToolUse':
        Path(values[0], 'heartbeat-' + values[1]).touch()
        return
    fields = ('hook_event_name', 'session_id', 'prompt', 'last_assistant_message', 'model', 'transcript_path',
              'source', 'reason', 'error', 'error_details')
    data = {key: event[key] for key in fields if event.get(key) is not None}
    data.update(role=values[1], time=time.time())
    events = Path(values[0], 'events')
    with tempfile.NamedTemporaryFile(mode='w', dir=events, prefix='.', delete=False) as file:
        json.dump(data, file, ensure_ascii=False)
        temp = Path(file.name)
    temp.replace(events / f'{time.time_ns()}-{values[1]}.json')


def run_agent(agent, mode):
    target = os.environ.get('TMUX_PANE')
    if target:
        option(target, '@workspace_agent', agent)
        option(target, '@workspace_thread', '')
        option(target, '@workspace_transcript', '')
        option(target, '@name', 'Claude' if agent == 'claude' else 'Codex')
        set_status(target, 'Starting')
    args = [agent]
    if mode == 'resume':
        args += ['--resume'] if agent == 'claude' else ['resume']
    elif mode == 'continue':
        args += ['--continue'] if agent == 'claude' else ['resume', '--last']
    try:
        subprocess.run(args, check=False)
    finally:
        if target:
            set_status(target, 'Ready')
        os.execvp('/bin/bash', ['/bin/bash', '-l'])


def start_agent(target, agent, mode):
    source = pane(target)
    available = next((p for p in panes(source['window'])
                      if p['pane_current_command'] == 'bash' and p['@name'].startswith('Agent') and
                      shell_idle(int(p['pane_pid']))), None)
    command = [SELF, 'run-agent', agent, mode]
    if available:
        identifier = available['pane_id']
        # Only replace an idle shell with no foreground job or child processes.
        tmux('respawn-pane', '-k', '-t', identifier, '-c', available['pane_current_path'], shlex.join(map(str, command)))
    else:
        identifier = new_pane(target, 'Claude' if agent == 'claude' else 'Codex', command)
    option(identifier, 'allow-passthrough', 'on')
    tmux('select-pane', '-t', identifier)


def shell_idle(pid):
    try:
        children = Path(f'/proc/{pid}/task/{pid}/children').read_text().strip()
        return not children
    except OSError:
        return False


def open_file(path, line=None, column=None):
    path = Path(path).resolve()
    if not path.exists():
        raise ValueError('File not found: ' + str(path))
    if line and path.is_file():
        spawn(['gnome-text-editor', path, f'+{line}:{column or 1}'])
    else:
        spawn(['xdg-open', path])


def parse_reference(word, folder):
    value = word.strip()
    line = column = None
    previous = None
    while previous != value:
        previous = value
        value = value.strip('`\'"()[]<>{}').rstrip('.,;!?')
        match = re.search(r':(\d+)(?::(\d+))?:?$', value)
        if match:
            line = int(match[1])
            column = int(match[2]) if match[2] else None
            value = value[:match.start()]
        value = value.rstrip(':')
    return Path(folder, os.path.expanduser(value)).resolve(), line, column


def find_file(target, content=False):
    root = Path(pane(target)['root'])
    query = dialog('entry', 'Search project' if content else 'Find file',
                   'Text to find:' if content else 'Part of the file name:')
    if not query:
        return
    if content:
        result = run(['rg', '--json', '--fixed-strings', '--max-count=30', '--', query, '.'],
                     cwd=root, check=False)
        matches = []
        for raw in result.stdout.splitlines():
            item = json.loads(raw)
            if item['type'] == 'match':
                data = item['data']
                name = data['path'].get('text')
                if name:
                    line = data['line_number']
                    matches.append((str(len(matches)), name, str(line), data['lines'].get('text', '').strip()))
                    if len(matches) >= 300:
                        break
        selected = choose('Search results', matches, ('File', 'Line', 'Text')) if matches else None
        if selected is not None:
            entry = matches[int(selected)]
            open_file(root / entry[1], int(entry[2]))
    else:
        result = run(['fdfind', '--type', 'f', '--hidden', '--exclude', '.git', '--fixed-strings',
                      '--', query, '.'], cwd=root, check=False)
        files = result.stdout.splitlines()[:500]
        selected = choose('Files', [(str(i), file) for i, file in enumerate(files)]) if files else None
        if selected is not None:
            open_file(root / files[int(selected)])
    if not (matches if content else files):
        info('No matches in this project.')


def git_root(target):
    folder = pane(target)['root']
    result = run(['git', '-C', folder, 'rev-parse', '--show-toplevel'], check=False)
    if result.returncode:
        raise ValueError('This workspace is not a Git repository.')
    return Path(result.stdout.strip())


def review(target):
    spawn(['meld', git_root(target)])


def worktree(target):
    root = git_root(target)
    branch = dialog('entry', 'Separate working folder', 'Name for the new branch:')
    if not branch:
        return
    result = run(['git', 'check-ref-format', '--branch', branch], check=False)
    if result.returncode or branch in ('main', 'master'):
        raise ValueError('Choose a valid new feature branch name.')
    base = pick_folder('Choose the parent folder for the separate checkout')
    if not base:
        return
    destination = Path(base) / (root.name + '-' + branch.replace('/', '-'))
    if destination.exists():
        raise ValueError('That folder already exists: ' + str(destination))
    result = run(['git', '-C', root, 'worktree', 'add', '-b', branch, destination], check=False)
    if result.returncode:
        raise ValueError(result.stderr.strip())
    remember(destination)
    spawn(['xdg-terminal-exec', '--dir=' + str(destination), str(SELF), 'open', destination])


def preview(target, path):
    source = pane(target)
    existing = next((p for p in panes(source['window']) if p['@name'] == 'Preview'), None)
    command = shlex.join([str(SELF), 'render-preview', str(path)])
    if existing:
        tmux('respawn-pane', '-k', '-t', existing['pane_id'], command)
    else:
        new_pane(target, 'Preview', [SELF, 'render-preview', path], percent=40)


def render_preview(path):
    path = Path(path)
    if not path.is_file():
        raise ValueError('Choose a text or Markdown file to preview.')
    command = ['glow', '-s', 'dark', '-w', '80', str(path)] if path.suffix == '.md' else \
              ['batcat', '--color=always', '--style=numbers', '--paging=never', '--', str(path)]
    result = run(command, check=False)
    STATE.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=STATE, suffix='.preview') as file:
        file.write(result.stdout or result.stderr)
        file.flush()
        subprocess.run(['less', '-R', '--mouse', '--wheel-lines=1', file.name])


def extract_item(item):
    kind = item.get('type', '')
    if kind == 'userMessage':
        text = '\n'.join(block.get('text', '') for block in item.get('content', []) if block.get('type') == 'text')
    elif kind == 'agentMessage':
        text = item.get('text', '')
    elif kind == 'commandExecution':
        text = item.get('command', '') + '\n' + (item.get('aggregatedOutput') or '')
    else:
        return ''
    return kind + '\n' + text + '\n\n'


def output_text(target):
    details = next(p for p in panes() if p['pane_id'] == target)
    if details['@workspace_thread']:
        try:
            api = AppServer()
            turns, cursor = [], None
            try:
                for _ in range(100):
                    args = {'threadId': details['@workspace_thread'], 'limit': 100, 'itemsView': 'full'}
                    if cursor:
                        args['cursor'] = cursor
                    result = api.request('thread/turns/list', args)
                    turns += result['data']
                    cursor = result.get('nextCursor')
                    if not cursor:
                        break
                if turns:
                    turns.sort(key=lambda turn: turn.get('startedAt') or 0)
                    return ''.join(extract_item(item) for turn in turns for item in turn.get('items', []))
            finally:
                api.close()
        except (OSError, KeyError, ValueError):
            pass
    transcript = details['@workspace_transcript']
    if transcript and Path(transcript).is_file():
        pieces = []
        with Path(transcript).open() as file:
            for line in file:
                try:
                    message = json.loads(line)
                    if message.get('type') in ('user', 'assistant'):
                        content = message.get('message', {}).get('content', [])
                        text = content if isinstance(content, str) else '\n'.join(c.get('text', '') for c in content if c.get('type') == 'text')
                        if text:
                            pieces.append(message['type'] + '\n' + text + '\n\n')
                except ValueError:
                    continue
        if pieces:
            return ''.join(pieces)
    return tmux('capture-pane', '-p', '-t', target, '-S', '-').stdout


def pane_output(target, action):
    text = output_text(target)
    if action == 'export':
        result = run(['zenity', '--file-selection', '--save', '--confirm-overwrite',
                      '--title=Save agent output', '--filename=agent-output.txt'], check=False)
        if result.returncode == 0:
            Path(result.stdout.strip()).write_text(text)
        return
    STATE.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', prefix='agent-output-', suffix='.txt', dir=STATE, delete=False) as file:
        file.write(text)
        path = Path(file.name)
    if action == 'search':
        query = dialog('entry', 'Search agent output', 'Text to find:')
        if query is None:
            path.unlink()
            return
        matches = [(str(i), str(i + 1), line) for i, line in enumerate(text.splitlines()) if query.casefold() in line.casefold()]
        selected = choose('Matching output', matches[:300], ('Line', 'Text')) if matches else None
        if selected is None:
            if not matches:
                info('No matches in this agent’s output.')
            path.unlink()
            return
        open_file(path, int(selected) + 1)
    else:
        open_file(path)


CODEX_SESSIONS = Path(os.environ.get('CODEX_HOME') or HOME / '.codex').expanduser() / 'sessions'
CLAUDE_TOOLS = {'Bash': ('ran', 'command'), 'Read': ('read', 'file_path'), 'Edit': ('edited', 'file_path'),
                'MultiEdit': ('edited', 'file_path'), 'Write': ('wrote', 'file_path'),
                'NotebookEdit': ('edited', 'notebook_path'), 'Grep': ('searched for', 'pattern'),
                'Glob': ('looked for', 'pattern'), 'WebFetch': ('fetched', 'url'),
                'WebSearch': ('searched the web for', 'query'), 'Agent': ('asked an agent to', 'description'),
                'Task': ('asked an agent to', 'description'), 'Skill': ('used the skill', 'skill'),
                'Monitor': ('watched', 'description'), 'TaskStop': ('stopped a background task', '')}
# The agent's own plumbing, not work worth reading about.
HIDDEN_TOOLS = {'ToolSearch'}
# Pasted into Claude Code, a path to an existing image can turn into an attached image and break
# the surrounding lines apart, so tool lines name images by file name only.
IMAGE_FILE = re.compile(r'\.(?:png|jpe?g|gif|webp|bmp|tiff?|heic|avif|svg)$', re.IGNORECASE)
IMAGE_PATH = re.compile(r'(?<![\w.~/-])(?:~|\.{0,2})?/(?:[^\s\'"`/]+/)*([^\s\'"`/]+\.(?:png|jpe?g|gif|webp|bmp|tiff?|heic|avif|svg))\b',
                        re.IGNORECASE)


def tidy(text):
    """Trailing spaces and blank lines at either end removed."""
    lines = [line.rstrip() for line in text.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    while lines and not lines[0]:
        lines.pop(0)
    return '\n'.join(lines)


def tool_line(verb, detail=''):
    """One line for something the agent did: '  • ran: npm test'."""
    detail = next((line.strip() for line in str(detail or '').splitlines() if line.strip()), '')
    detail = IMAGE_PATH.sub(r'\1', home_path(detail))
    if len(detail) > 160:
        detail = detail[:159] + '…'
    return '  • ' + verb + (': ' + detail if detail else '')


def claude_prompt(text):
    """What the person typed, without Claude Code's wrappers; '' for Claude's own messages."""
    text = re.sub(r'<system-reminder>.*?</system-reminder>', '', text, flags=re.S)
    if re.match(r'\s*<(task-notification|local-command-stdout|local-command-stderr)>', text):
        return ''
    command = re.search(r'<command-name>(.*?)</command-name>', text, re.S)
    if command:
        arguments = re.search(r'<command-args>(.*?)</command-args>', text, re.S)
        return (command[1].strip() + ' ' + (arguments[1].strip() if arguments else '')).strip()
    shell = re.search(r'<bash-input>(.*?)</bash-input>', text, re.S)
    if shell:
        return '! ' + shell[1].strip()
    text = re.sub(r'</?(pasted_content|bash-stdout|bash-stderr)\b[^>]*>', '', text)
    return text.strip()


def claude_conversation(path):
    """[speaker, [text, ...]] entries from a Claude Code transcript, in order."""
    entries, queued, typed = [], [], []

    def add(speaker, text):
        if not text:
            return
        if entries and entries[-1][0] == speaker:
            entries[-1][1].append(text)
        else:
            entries.append([speaker, [text]])

    with Path(path).open(errors='replace') as file:
        for line in file:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if not isinstance(message, dict) or message.get('isSidechain'):
                continue
            kind = message.get('type')
            if kind == 'system' and message.get('subtype') == 'compact_boundary':
                add('', '(The conversation was compacted here.)')
            elif kind == 'attachment':
                # A message typed while Claude was busy reaches it mid-turn as an attachment.
                item = message.get('attachment') or {}
                if item.get('type') == 'queued_command' and (item.get('origin') or {}).get('kind') == 'human' \
                        and isinstance(item.get('prompt'), str):
                    text = claude_prompt(item['prompt'])
                    if text in typed:
                        typed.remove(text)
                    elif text:
                        queued.append(text)
                        add('You', text)
            elif kind == 'user' and not (message.get('isMeta') or message.get('isCompactSummary')
                                         or message.get('isVisibleInTranscriptOnly')):
                content = message.get('message', {}).get('content')
                blocks = [{'type': 'text', 'text': content}] if isinstance(content, str) else content or []
                parts = [claude_prompt(b.get('text', '')) if b.get('type') == 'text' else
                         '[image]' if b.get('type') == 'image' else '' for b in blocks if isinstance(b, dict)]
                text = '\n'.join(part for part in parts if part)
                if text in queued:
                    queued.remove(text)
                elif text:
                    typed.append(text)
                    add('You', text)
            elif kind == 'assistant':
                for block in message.get('message', {}).get('content') or []:
                    if not isinstance(block, dict):
                        continue
                    if block.get('type') == 'text' and block.get('text', '').strip():
                        add('Claude', block['text'].strip())
                    elif block.get('type') == 'tool_use' and block.get('name') not in HIDDEN_TOOLS:
                        verb, field = CLAUDE_TOOLS.get(block.get('name'), (block.get('name', 'tool'), ''))
                        detail = (block.get('input') or {}).get(field, '') if field else ''
                        if block.get('name') == 'Read' and IMAGE_FILE.search(str(detail)):
                            verb, detail = 'viewed image', Path(str(detail)).name
                        add('Claude', tool_line(verb, detail))
    return entries


def codex_entries(items):
    """[speaker, [text, ...]] entries from Codex conversation items: the Codex server's
    (userMessage, agentMessage, …) or a saved session file's (UserMessage, AgentMessage, …)."""
    entries = []

    def add(speaker, text):
        if text:
            if entries and entries[-1][0] == speaker:
                entries[-1][1].append(text)
            else:
                entries.append([speaker, [text]])

    def texts(content):
        return '\n'.join(block.get('text', '') if block.get('type', '').lower() == 'text' else '[image]'
                         for block in content or [] if isinstance(block, dict)).strip()

    for item in items:
        kind = str(item.get('type', '')).lower()
        if kind == 'usermessage':
            add('You', texts(item.get('content')))
        elif kind == 'agentmessage':
            add('Codex', (item.get('text') or texts(item.get('content'))).strip())
        elif kind == 'commandexecution':
            command = item.get('command', '')
            actions = item.get('commandActions') or []
            if actions and actions[0].get('command'):
                command = actions[0]['command']
            elif isinstance(command, list):
                command = command[2] if len(command) > 2 and command[1] in ('-lc', '-c') else shlex.join(command)
            add('Codex', tool_line('ran', command))
        elif kind == 'filechange':
            changes = item.get('changes') or []
            paths = list(changes) if isinstance(changes, dict) else [c.get('path', '') for c in changes]
            add('Codex', tool_line('edited', ', '.join(home_path(p) for p in paths if p)))
        elif kind == 'websearch':
            add('Codex', tool_line('searched the web for', item.get('query', '')))
        elif kind == 'imageview':
            add('Codex', tool_line('viewed image', Path(item['path']).name if item.get('path') else ''))
        elif kind == 'contextcompaction':
            add('', '(The conversation was compacted here.)')
    return entries


def open_session_file(pid):
    """The Codex session file a process (or one of its children) is writing, if any."""
    pending, seen = [str(pid)], set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        with contextlib.suppress(OSError):
            for fd in Path('/proc', current, 'fd').iterdir():
                with contextlib.suppress(OSError):
                    target = os.readlink(fd)
                    if target.startswith(str(CODEX_SESSIONS) + '/') and target.endswith('.jsonl'):
                        return Path(target)
        with contextlib.suppress(OSError):
            pending += Path('/proc', current, 'task', current, 'children').read_text().split()
    return None


def codex_conversation(details):
    """Entries from the pane's Codex conversation: its saved session file, else the Codex server."""
    thread = details['@workspace_thread']
    files = sorted(CODEX_SESSIONS.glob(f'*/*/*/rollout-*-{thread}.jsonl')) if thread else []
    session = files[-1] if files else open_session_file(details['pane_pid'])
    if session and session.is_file():
        items = []
        with session.open(errors='replace') as file:
            for line in file:
                with contextlib.suppress(ValueError, AttributeError):
                    record = json.loads(line)
                    payload = record.get('payload') or {}
                    if record.get('type') == 'event_msg' and payload.get('type') == 'item_completed':
                        items.append(payload.get('item') or {})
        if items:
            return codex_entries(items)
    if thread:
        with contextlib.suppress(OSError, KeyError, ValueError):
            api = AppServer()
            try:
                turns, cursor = [], None
                for _ in range(100):
                    arguments = {'threadId': thread, 'limit': 100, 'itemsView': 'full'}
                    if cursor:
                        arguments['cursor'] = cursor
                    result = api.request('thread/turns/list', arguments)
                    turns += result['data']
                    cursor = result.get('nextCursor')
                    if not cursor:
                        break
            finally:
                api.close()
            turns.sort(key=lambda turn: turn.get('startedAt') or 0)
            return codex_entries([item for turn in turns for item in turn.get('items', [])])
    return []


def render_conversation(agent, folder, entries):
    lines = [f'Conversation with {agent} in {home_path(folder)} '
             f'(copied {time.strftime("%Y-%m-%d %H:%M")})']
    for speaker, parts in entries:
        lines.append('')
        if speaker:
            lines.append(speaker + ':')
        # A step repeated back to back (the same file read four times) is one line with a count.
        folded = []
        for part in parts:
            if folded and part.startswith('  • ') and folded[-1][0] == part:
                folded[-1][1] += 1
            else:
                folded.append([part, 1])
        for index, (part, times) in enumerate(folded):
            if times > 1:
                part += f' (×{times})'
            if index and not part.startswith('  • '):
                lines.append('')
            lines.append(part)
    return '\n'.join(lines) + '\n'


def copy_everything(target):
    """Right-click → Copy everything: an agent's whole conversation, or a terminal's whole history."""
    details = next((p for p in panes() if p['pane_id'] == target), None)
    if details is None:
        raise ValueError('This pane has closed.')
    agent = details['pane_current_command'] if details['pane_current_command'] in ('claude', 'codex') else \
        private_agent(details['pane_pid'])
    text, note = '', ''
    if agent in ('claude', 'codex'):
        name = agent.title()
        entries = []
        if agent == 'claude' and details['@workspace_transcript'] and Path(details['@workspace_transcript']).is_file():
            entries = claude_conversation(details['@workspace_transcript'])
        elif agent == 'codex':
            entries = codex_conversation(details)
        if entries:
            text = render_conversation(name, details['pane_current_path'], entries)
            messages = sum(1 for speaker, _ in entries if speaker)
            note = f'Copied the whole conversation with {name} ({messages} messages). Paste with Ctrl+V.'
        else:
            note = f'Copied what is on screen: {name}’s full conversation was not found. Paste with Ctrl+V.'
    if not text:
        # Joined lines, so wrapped commands and output paste as they were typed and printed.
        text = tidy(tmux('capture-pane', '-p', '-J', '-t', target, '-S', '-', '-E', '-').stdout)
        if text:
            text += '\n'
            note = note or f'Copied everything in this terminal ({text.count(chr(10))} lines). Paste with Ctrl+V.'
    if not text:
        tmux('display-message', '-d', '3000', '-t', target, 'Nothing to copy yet.', check=False)
        return
    subprocess.run(['wl-copy'], input=text, text=True, check=False, timeout=15,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    tmux('display-message', '-d', '4000', '-t', target, note, check=False)


def resources(target):
    source = pane(target)
    existing = tmux('list-windows', '-t', source['session'], '-F', '#{window_id}' + SEP + '#{window_name}').stdout
    for row in existing.splitlines():
        identifier, name = row.split(SEP, 1)
        if name == 'Resources':
            tmux('select-window', '-t', identifier)
            return
    identifier = tmux('new-window', '-P', '-F', '#{window_id}', '-t', source['session'], '-n', 'Resources',
                      '-c', source['root'], 'htop').stdout.strip()
    monitor = 'watch -n 2 nvidia-smi' if shutil.which('nvidia-smi') else shlex.join(
        ['bash', '-c', "printf '%s\\n' 'NVIDIA telemetry is unavailable on this computer.' 'Press Enter to close this panel.'; read -r"])
    gpu = tmux('split-window', '-v', '-t', identifier, '-l', '45%',
               '-P', '-F', '#{pane_id}', monitor).stdout.strip()
    option(gpu, '@name', 'GPU')
    tmux('display-message', 'Resources: right-click → Return to agents')


def layout(target, kind):
    source = pane(target)
    all_panes = panes(source['window'])
    if kind == 'one':
        if tmux('display-message', '-p', '-t', target, '#{window_zoomed_flag}').stdout.strip() != '1':
            tmux('resize-pane', '-Z', '-t', target)
    elif kind == 'two':
        agents = [p for p in all_panes if p['@name'] != 'Directory']
        if len(agents) < 2:
            new_pane(target, 'Agent 2')
        tmux('resize-pane', '-Z', '-t', target) if tmux('display-message', '-p', '-t', target, '#{window_zoomed_flag}').stdout.strip() == '1' else None
        reset_layout(target)
    elif kind == 'logs':
        result = run(['zenity', '--file-selection', '--title=Choose a log file'], check=False)
        if result.returncode == 0:
            new_pane(target, 'Logs', ['tail', '-n', '100', '-F', '--', result.stdout.strip()], vertical=True, percent=30)
    elif kind == 'review':
        root = git_root(target)
        new_pane(target, 'Changes', ['git', '-C', root, '-c', 'core.pager=less -R --mouse --wheel-lines=1',
                                   'diff', 'HEAD'], vertical=True, percent=40)
        review(target)
    elif kind == 'reset':
        reset_layout(target)


def reset_layout(target):
    source = pane(target)
    tmux('select-layout', '-t', source['window'], 'even-horizontal')
    directory = next((p for p in panes(source['window']) if p['@name'] == 'Directory'), None)
    if directory:
        width = int(tmux('display-message', '-p', '-t', target, '#{window_width}').stdout.strip())
        tmux('resize-pane', '-t', directory['pane_id'], '-x', str(max(20, width * 24 // 100)))


def sidebar(folder, selected, target, instance):
    selected_path = Path(selected) if selected else None
    source = pane(target)
    choices = [('copy', 'Copy full path'), ('rename', 'Rename selected file or folder'),
               ('mkdir', 'New folder'), ('hidden', 'Show / hide hidden files'),
               ('send', 'Paste selected path into an agent'), ('preview', 'Preview selected file'),
               ('find', 'Find a file'), ('search', 'Search inside project files'),
               ('review', 'Review Git changes'), ('fav', 'Add / remove project favourite')]
    action = choose('Directory actions', choices)
    if not action:
        return
    if action == 'copy' and selected_path:
        run(['wl-copy'], input=str(selected_path))
    elif action == 'rename' and selected_path:
        name = dialog('entry', 'Rename', 'New name:', '--entry-text=' + selected_path.name)
        if name:
            if name in ('.', '..') or '/' in name or '\0' in name:
                raise ValueError('Enter a file name without a slash.')
            destination = selected_path.with_name(name)
            if destination.exists() or destination.is_symlink():
                raise ValueError('That name already exists.')
            selected_path.rename(destination)
    elif action == 'mkdir':
        name = dialog('entry', 'New folder', 'Folder name:')
        if name:
            if name in ('.', '..') or '/' in name or '\0' in name:
                raise ValueError('Enter a folder name without a slash.')
            (Path(folder) / name).mkdir()
    elif action == 'hidden':
        run([YA, 'emit-to', instance, 'hidden', 'toggle'])
    elif action == 'send' and selected_path:
        candidates = [p for p in panes(source['window']) if p['@name'] != 'Directory']
        recipient = choose('Paste path into which agent?', [(p['pane_id'], p['@name'] or p['pane_current_command']) for p in candidates])
        if recipient:
            buffer = 'workspace-' + uuid.uuid4().hex
            tmux('load-buffer', '-b', buffer, '-', input=shlex.quote(str(selected_path)))
            tmux('paste-buffer', '-p', '-d', '-b', buffer, '-t', recipient)
            tmux('select-pane', '-t', recipient)
    elif action == 'preview' and selected_path:
        preview(target, selected_path)
    elif action in ('find', 'search'):
        find_file(target, action == 'search')
    elif action == 'review':
        review(target)
    elif action == 'fav':
        toggle_favourite(target)


def toggle_favourite(target):
    folder = pane(target)['root']
    old = next((p for p in catalog()['projects'] if p['path'] == folder), {})
    remember(folder, not old.get('favourite', False))
    tmux('display-message', '-t', target, 'Favourite removed' if old.get('favourite') else 'Added to favourites')


def snapshot():
    live = sessions()
    if not live:
        return
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / 'snapshot.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        metadata = {'sessions': live, 'panes': []}
        for s in live:
            for p in panes(s['session_id'], all_windows=True):
                index = tmux('display-message', '-p', '-t', p['pane_id'], '#{window_index}' + SEP + '#{pane_index}').stdout.strip()
                metadata['panes'].append({'session_name': s['session_name'], 'index': index,
                                          'name': p['@name'], 'agent': p['@workspace_agent']})
        socket_path = tmux('display-message', '-p', '#{socket_path}').stdout.strip()
        pid = tmux('display-message', '-p', '#{pid}').stdout.strip()
        env = dict(os.environ, TMUX=socket_path + ',' + pid + ',0')
        # tmux-resurrect names snapshots to the second. Saving twice in that
        # second compares the file to itself and deletes the active snapshot.
        saved = STATE / 'resurrect/last'
        if saved.is_symlink() and saved.resolve().name == time.strftime('tmux_resurrect_%Y%m%dT%H%M%S.txt'):
            time.sleep(1.05 - time.time() % 1)
        run([RESURRECT / 'save.sh', 'quiet'], env=env, umask=0o077)
        if not saved.is_file():
            raise OSError('Workspace layout was not saved; the previous snapshot may need checking.')
        atomic_json(STATE / 'layout-metadata.json', metadata)


def restore():
    saved = STATE / 'resurrect/last'
    if not saved.exists():
        info('No saved workspace layouts yet.')
        return
    if sessions():
        info('Your workspaces are already running. Open or reopen them from the project list.')
        return
    seed = tmux('-f', CONFIG, 'new-session', '-d', '-P', '-F', '#{session_id}', '-s', 'restore-seed').stdout.strip()
    socket_path = tmux('display-message', '-p', '#{socket_path}').stdout.strip()
    pid = tmux('display-message', '-p', '#{pid}').stdout.strip()
    env = dict(os.environ, TMUX=socket_path + ',' + pid + ',0')
    run([RESURRECT / 'restore.sh'], env=env)
    tmux('kill-session', '-t', seed, check=False)
    try:
        metadata = json.loads((STATE / 'layout-metadata.json').read_text())
    except (OSError, ValueError):
        metadata = {'panes': []}
    for entry in metadata['panes']:
        window, number = entry['index'].split(SEP)
        target = entry['session_name'] + ':' + window + '.' + number
        option(target, '@name', entry['name'])
        if entry['name'] == 'Directory':
            tmux('respawn-pane', '-k', '-t', target, str(HOME / '.local/bin/work-files'), check=False)
        else:
            option(target, 'allow-passthrough', 'on')
            option(target, '@workspace_status', 'Ready')
    for s in sessions():
        old = next((entry for entry in metadata.get('sessions', []) if entry['session_name'] == s['session_name']), None)
        tmux('set-option', '-t', s['session_id'], '@workspace_label', workspace_label(old or s))
        remember(s['session_path'])


def action(target, name):
    source = pane(target)
    if name == 'paste':
        # A named buffer avoids races with another pane's copy/paste operation.
        result = run(['wl-paste', '--no-newline'], check=False)
        if result.returncode == 0 and result.stdout:
            buffer = 'workspace-' + uuid.uuid4().hex
            tmux('load-buffer', '-b', buffer, '-', input=result.stdout)
            tmux('paste-buffer', '-p', '-d', '-b', buffer, '-t', target)
    elif name == 'copy-all':
        copy_everything(target)
    elif name in ('split-right', 'split-down'):
        new_pane(target, 'Terminal', vertical=name == 'split-down')
    elif name.endswith('-menu'):
        menus = {
            'agents-menu': [('pair', 'Pair two agents: orchestrator + coder…'),
                            ('claude-new', 'Start Claude'), ('codex-new', 'Start Codex'),
                            ('claude-resume', 'Choose a Claude conversation'), ('codex-resume', 'Choose a Codex conversation'),
                            ('claude-continue', 'Continue latest Claude conversation'), ('codex-continue', 'Continue latest Codex conversation')],
            'project-menu': [('find', 'Find a file'), ('search', 'Search inside project files'),
                             ('review', 'Review Git changes'), ('favourite', 'Add / remove favourite'),
                             ('worktree', 'Create a separate working folder')],
            'view-menu': [('layouts', 'Choose layout / reset pane sizes'), ('directory', 'Show Directory'),
                          ('preview-close', 'Close preview panel'), ('output', 'Read agent output in Text Editor'),
                          ('search-output', 'Search agent output'), ('export-output', 'Save agent output'),
                          ('history', 'Search command history'), ('resources', 'CPU, RAM and GPU usage'),
                          ('agents-window', 'Return to agents')],
        }
        selected = choose(name.replace('-menu', '').title(), menus[name])
        if selected:
            action(target, selected)
    elif name == 'pair':
        run([HOME / '.local/bin/agent-pair', 'action', target], check=False)
    elif name.startswith(('claude-', 'codex-')):
        agent, mode = name.split('-', 1)
        start_agent(target, agent, mode)
    elif name == 'projects':
        selected = picker()
        if selected:
            env = {key: value for key, value in os.environ.items() if key not in ('TMUX', 'TMUX_PANE')}
            spawn(workspace_args(selected), env=env)
    elif name == 'favourite':
        toggle_favourite(target)
    elif name == 'park':
        snapshot()
        tmux('set-option', '-t', source['session'], 'destroy-unattached', 'off')
        tmux('detach-client', '-s', source['session'])
    elif name == 'review':
        review(target)
    elif name in ('find', 'search'):
        find_file(target, name == 'search')
    elif name in ('output', 'search-output', 'export-output'):
        pane_output(target, {'output': 'view', 'search-output': 'search', 'export-output': 'export'}[name])
    elif name == 'resources':
        resources(target)
    elif name == 'agents-window':
        windows = records('list-windows', ['window_id', 'window_name'], '-t', source['session'])
        candidates = [w for w in windows if w['window_name'] != 'Resources']
        if candidates:
            tmux('select-window', '-t', candidates[0]['window_id'])
    elif name == 'layouts':
        selected = choose('Workspace layout', [('one', 'One agent — enlarge this pane'),
                                               ('two', 'Two agents'), ('logs', 'Agents + a log file'),
                                               ('review', 'Review mode'), ('reset', 'Reset pane sizes')])
        if selected:
            layout(target, selected)
    elif name == 'preview-close':
        existing = next((p for p in panes(source['window']) if p['@name'] == 'Preview'), None)
        if existing:
            tmux('kill-pane', '-t', existing['pane_id'])
    elif name == 'worktree':
        worktree(target)
    elif name == 'save':
        snapshot()
        tmux('display-message', '-t', target, 'Workspace layouts saved')
    elif name == 'history':
        new_pane(target, 'History', [SELF, 'show-history', target])
    elif name == 'directory':
        existing = next((p for p in panes(source['window']) if p['@name'] == 'Directory'), None)
        if existing:
            tmux('select-pane', '-t', existing['pane_id'])
        else:
            identifier = tmux('split-window', '-hbf', '-l', '24%', '-t', target, '-P', '-F', '#{pane_id}',
                              '-c', source['root'], str(HOME / '.local/bin/work-files')).stdout.strip()
            option(identifier, '@name', 'Directory')


def shell_start(target):
    if os.environ.get('TMUX', '').split(',')[0] != tmux('display-message', '-p', '#{socket_path}', check=False).stdout.strip():
        return
    option(target, '@workspace_started', str(time.time()))
    set_status(target, 'Working')


def show_history(target):
    folder = pane(target)['cwd']
    env = dict(os.environ, PWD=folder, ATUIN_SESSION=uuid.uuid4().hex,
               ATUIN_SHELL_BASH='t', ATUIN_LOG='error')
    result = subprocess.run(['atuin', 'search', '-i', '--filter-mode', 'directory'],
                            stderr=subprocess.PIPE, text=True, env=env, cwd=folder)
    selected = result.stderr.strip().removeprefix('__atuin_accept__:')
    if result.returncode == 0 and selected:
        buffer = 'workspace-' + uuid.uuid4().hex
        tmux('load-buffer', '-b', buffer, '-', input=selected)
        tmux('paste-buffer', '-p', '-d', '-b', buffer, '-t', target)
        tmux('select-pane', '-t', target)


def shell_done(target, code):
    if os.environ.get('TMUX', '').split(',')[0] != tmux('display-message', '-p', '#{socket_path}', check=False).stdout.strip():
        return
    value = tmux('show-option', '-p', '-v', '-t', target, '@workspace_started', check=False).stdout.strip()
    elapsed = time.time() - float(value) if value else 0
    option(target, '@workspace_started', '')
    set_status(target, ('Error' if code else 'Finished') if elapsed >= 10 else 'Ready', attention=elapsed >= 10)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    opening = sub.add_parser('open')
    opening.add_argument('folder', nargs='?')
    opening.add_argument('--pick', action='store_true')
    opening_mode = opening.add_mutually_exclusive_group()
    opening_mode.add_argument('--new', action='store_true')
    opening_mode.add_argument('--session')
    sub.add_parser('monitor')
    sub.add_parser('snapshot')
    sub.add_parser('restore')
    sub.add_parser('hook')
    status = sub.add_parser('status-once')
    status.add_argument('--offline', action='store_true')
    acting = sub.add_parser('action')
    acting.add_argument('pane')
    acting.add_argument('name')
    runner = sub.add_parser('run-agent')
    runner.add_argument('agent', choices=('claude', 'codex'))
    runner.add_argument('mode', choices=('new', 'resume', 'continue'))
    bar = sub.add_parser('sidebar')
    for field in ('folder', 'selected', 'pane', 'instance'):
        bar.add_argument(field)
    renderer = sub.add_parser('render-preview')
    renderer.add_argument('path')
    starting = sub.add_parser('shell-start')
    starting.add_argument('pane')
    ending = sub.add_parser('shell-done')
    ending.add_argument('pane')
    ending.add_argument('code', type=int)
    history = sub.add_parser('show-history')
    history.add_argument('pane')
    args = parser.parse_args()
    try:
        if args.command == 'open':
            open_workspace(args.folder, args.pick, args.new, args.session)
        elif args.command == 'action':
            action(args.pane, args.name)
        elif args.command == 'run-agent':
            run_agent(args.agent, args.mode)
        elif args.command == 'sidebar':
            sidebar(args.folder, args.selected, args.pane, args.instance)
        elif args.command == 'render-preview':
            render_preview(args.path)
        elif args.command == 'shell-start':
            shell_start(args.pane)
        elif args.command == 'shell-done':
            shell_done(args.pane, args.code)
        elif args.command == 'show-history':
            show_history(args.pane)
        elif args.command == 'status-once':
            api = None if args.offline else AppServer()
            try:
                monitor_once(api)
            finally:
                if api:
                    api.close()
        else:
            globals()[args.command]()
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        if args.command in ('hook', 'monitor', 'snapshot', 'shell-start', 'shell-done'):
            print(str(exc), file=sys.stderr)
        else:
            error(str(exc))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
