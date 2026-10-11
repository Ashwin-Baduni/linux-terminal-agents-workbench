#!/usr/bin/env python3
"""Exercise the staged workspace on a disposable tmux socket and test folder."""
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import support  # noqa: E402

root = support.home()
spec = importlib.util.spec_from_file_location('workspace', root / '.local/share/workspace/workspace.py')
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)

assert w.codex_title_name('⠇ Example task | Project') == 'Example task'
assert w.codex_title_name('[ ! ] Action Required | Example task | Project') == 'Example task'
assert w.codex_title_name('✓ Task | with separator | Project') == 'Task | with separator'
assert w.status_for_thread({'status': {'type': 'active', 'activeFlags': ['waitingOnUserInput']}}) == 'Needs input'
assert w.home_path(str(root / 'Desktop/Project')) == '~/Desktop/Project' and w.home_path('/srv/x') == '/srv/x'
assert w.home_path(str(root) + '-other/x') == str(root) + '-other/x'


def wait_for(predicate):
    for _ in range(50):
        if predicate():
            return
        time.sleep(.1)
    raise AssertionError('A workspace operation did not complete in five seconds.')


with tempfile.TemporaryDirectory(prefix='workspace-check-') as temporary:
    scratch = Path(temporary)
    w.STATE = scratch / 'state'
    w.SOCKET = str(scratch / 'socket')
    w.CONFIG = scratch / 'tmux.conf'
    w.CONFIG.write_text((root / '.config/tmux/workspace.conf').read_text().replace(
        str(root / '.local/state/workspace/resurrect'), str(w.STATE / 'resurrect')))
    os.environ['WORKSPACE_TMUX_SOCKET'] = w.SOCKET
    os.environ['WORKSPACE_STATE_DIR'] = str(w.STATE)
    project = scratch / "Project with spaces and ' quotes"
    project.mkdir()
    (project / 'example.py').write_text('first\nneedle on line 2\n')
    clients = []

    def attach(session, replace=False):
        args = ['tmux', '-S', w.SOCKET, '-C', 'attach-session']
        if replace:
            args.append('-d')
        process = subprocess.Popen(args + ['-t', session], stdin=subprocess.PIPE,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        clients.append(process)
        wait_for(lambda: any(s['session_id'] == session and int(s['session_attached']) for s in w.sessions()))
        assert process.poll() is None
        return process

    try:
        first = w.ensure_session(project)
        a = attach(first)
        before = {p['pane_id']: p['pane_pid'] for p in w.panes(first)}
        assert len(before) == 2  # Agent 1 and Agent 2; Directory opens from the menu.
        assert w.ensure_session(project) == first
        b = attach(first, replace=True)
        wait_for(lambda: a.poll() is not None)
        assert before == {p['pane_id']: p['pane_pid'] for p in w.panes(first)}
        print('PASS: reopening preserves agents and pane processes', flush=True)

        second = w.ensure_session(project, new=True)
        c = attach(second)
        rows, _ = w.workspace_rows(w.sessions(), w.catalog()['projects'])
        assert len(rows) == 2 and len({row[3] for row in rows}) == 2
        protected = {p['pane_id']: p['pane_pid'] for p in w.panes(second)}
        b.send_signal(signal.SIGHUP)
        b.wait(timeout=5)
        wait_for(lambda: not any(s['session_id'] == first for s in w.sessions()))
        assert protected == {p['pane_id']: p['pane_pid'] for p in w.panes(second)}
        print('PASS: two workspaces in one folder; closing one preserves the other', flush=True)

        target = next(p['pane_id'] for p in w.panes(second) if p['@name'] == 'Agent 1')
        w.action(target, 'park')
        c.wait(timeout=5)
        assert w.sessions()[0]['session_attached'] == '0'
        d = attach(second)
        assert w.tmux('show-option', '-v', '-t', second, 'destroy-unattached').stdout.strip() == 'on'
        print('PASS: explicit Park survives; reopen restores close-on-X', flush=True)

        with patch.object(w, 'dialog', return_value='needle'), patch.object(w, 'choose', return_value='0'), patch.object(w, 'open_file') as opened:
            w.find_file(target, content=True)
            assert opened.call_args.args == (project / 'example.py', 2)
        parsed, line, column = w.parse_reference('example.py:2:3', project)
        assert (parsed, line, column) == (project / 'example.py', 2, 3)
        print('PASS: project search and exact-line references', flush=True)

        # A menu opened in one window must keep targeting that window even
        # when another window becomes active before the menu returns.
        other = w.tmux('new-window', '-t', second, '-P', '-F', '#{pane_id}',
                       '-c', project, 'sleep 60').stdout.strip()
        w.action(target, 'split-right')
        assert len(w.panes(w.pane(target)['window'])) == 3
        assert len(w.panes(w.pane(other)['window'])) == 1
        split = next(p['pane_id'] for p in w.panes(w.pane(target)['window']) if p['@name'] == 'Terminal')
        wait_for(lambda: w.pane(split)['cwd'] == str(project))
        w.tmux('kill-pane', '-t', split)
        w.tmux('kill-pane', '-t', other)
        w.layout(target, 'one')
        w.layout(target, 'one')
        assert w.tmux('display-message', '-p', '-t', target, '#{window_zoomed_flag}').stdout.strip() == '1'
        w.layout(target, 'two')
        assert w.tmux('display-message', '-p', '-t', target, '#{window_zoomed_flag}').stdout.strip() == '0'
        print('PASS: quoted-folder splits, target window, repeatable layouts', flush=True)

        # Window title: label, folder, then every agent/terminal state left to right.
        names = {p['@name']: p['pane_id'] for p in w.panes(second)}
        label = w.tmux('show-option', '-v', '-t', second, '@workspace_label').stdout.strip()
        w.set_status(names['Agent 1'], 'Needs input')
        w.set_status(names['Agent 2'], 'Finished')
        title = w.tmux('display-message', '-p', '-t', second, '#{T:set-titles-string}').stdout.strip()
        assert title == label + ' — ' + w.home_path(str(project)) + ' — 🔴 Needs you · 🟢 Finished', title
        print('PASS: window title lists each agent state', flush=True)

        w.action(names['Agent 1'], 'directory')  # Right-click → Show Directory.
        directory = next(p for p in w.panes(second) if p['@name'] == 'Directory')
        assert w.pane(directory['pane_id'])['window'] == w.pane(names['Agent 1'])['window']
        w.action(names['Agent 1'], 'directory')  # A second click selects it rather than adding another.
        assert sum(p['@name'] == 'Directory' for p in w.panes(second)) == 1
        print('PASS: workspace opens with two agents; Show Directory adds the browser once', flush=True)

        w.snapshot()
        w.snapshot()
        assert (w.STATE / 'resurrect/last').exists()
        expected = {p['@name'] for p in w.panes(second)}
        w.tmux('kill-server')
        d.wait(timeout=5)
        time.sleep(.2)
        w.restore()
        assert len(w.sessions()) == 1
        assert {p['@name'] for p in w.panes()} == expected
        wait_for(lambda: all(p['pane_current_path'] == str(project) for p in w.panes()))
        print('PASS: snapshot and restore recover folders, labels and panes', flush=True)

        directory = next(p for p in w.panes() if p['@name'] == 'Directory')
        wait_for(lambda: 'Actions' in w.tmux('capture-pane', '-p', '-t', directory['pane_id']).stdout)
        print('PASS: Directory browser loads its custom buttons', flush=True)
    finally:
        w.tmux('kill-server', check=False)
        for process in clients:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)

print('Automated workspace checks passed. Visual and login checks still need a real desktop session.')
