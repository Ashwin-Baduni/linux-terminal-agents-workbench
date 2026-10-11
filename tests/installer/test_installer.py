#!/usr/bin/env python3
"""The installer, run against scratch homes: preservation, repeat runs, safety and rollback.

Host commands that would change this machine (sudo, apt, snap, gsettings, systemctl,
cache tools) are replaced by traps that record any call; staging must make none. The
desktop tests use stand-ins for gsettings and systemctl that keep their state in a file.
"""
import contextlib
import http.server
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import tomllib
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import support  # noqa: E402
from installer import packages, preflight, transaction  # noqa: E402
from installer.desktop import Desktop  # noqa: E402
from installer.engine import Options, Run, resolve_values, rollback  # noqa: E402
from installer.fsops import Home, Unsafe  # noqa: E402
from installer.manifest import load  # noqa: E402
from installer.plan import Context, build  # noqa: E402
from installer.stage import stage  # noqa: E402

support.home()
REPO = support.REPO
INSTALL = str(REPO / 'install.sh')
UUID = 'clipboard-list@linux-terminal-agents-workbench'
TRAPPED = ('sudo', 'apt-get', 'apt', 'snap', 'gsettings', 'systemctl', 'dconf', 'update-desktop-database',
           'batcat', 'curl', 'wget', 'gnome-extensions')
LEGACY_BLOCK = '''# --- Terminal tools (an earlier version of this block) ---
export FZF_DEFAULT_OPTS='--height 40%'
eval "$(starship init bash)"
if [[ -n "$TMUX_PANE" && -n "$TMUX" ]]; then
\tpreexec_functions+=(__workspace_preexec)
\tprecmd_functions+=(__workspace_prompt)
fi
'''


def snapshot(home):
    """Everything in a home except the installer's own records: path -> (kind, mode, bytes or link)."""
    found = {}
    for path in sorted(Path(home).rglob('*')):
        rel = str(path.relative_to(home))
        if rel.startswith('.local/state/workbench') or rel in ('.local', '.local/state'):
            continue    # the installer's own records (and the folders holding them) stay
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            found[rel] = ('link', None, os.readlink(path))
        elif stat.S_ISDIR(info.st_mode):
            found[rel] = ('dir', None, None)
        else:
            found[rel] = ('file', stat.S_IMODE(info.st_mode), path.read_bytes())
    return found


class Scratch(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='workbench-installer-'))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.home = self.root / 'home'
        self.home.mkdir()
        traps = self.root / 'traps'
        traps.mkdir()
        self.trap_log = self.root / 'trapped.log'
        for name in TRAPPED:
            (traps / name).write_text(f'#!/bin/sh\necho "{name} $*" >> {self.trap_log}\nexit 1\n')
            (traps / name).chmod(0o755)
        self.env = dict(os.environ, PATH=f'{traps}:{os.environ["PATH"]}')

    def cli(self, *args, ok=True):
        result = subprocess.run([INSTALL, *args, '--target-home', str(self.home)], env=self.env,
                                capture_output=True, text=True, timeout=300)
        if ok is not None:
            self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
        return result

    def assertNothingTrapped(self):
        self.assertFalse(self.trap_log.exists(), self.trap_log.read_text() if self.trap_log.exists() else '')

    def write(self, rel, text, mode=0o644):
        path = self.home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(mode)
        return path

    def read(self, rel):
        return (self.home / rel).read_text()

    def backups(self):
        folder = self.home / '.local/state/workbench/backups'
        return sorted(folder.iterdir()) if folder.is_dir() else []


class Staging(Scratch):
    def test_check_changes_nothing(self):
        before = snapshot(self.home)
        out = self.cli('--check').stdout
        self.assertIn('Files (', out)
        self.assertIn('Nothing was changed', out)
        self.assertEqual(snapshot(self.home), before)
        self.assertFalse((self.home / '.local').exists())
        self.assertNothingTrapped()

    def test_install_then_repeat_changes_nothing(self):
        out = self.cli('--install').stdout
        self.assertIn('Installed. Backup and journal:', out)
        self.assertTrue((self.home / '.local/bin/workspace-control').is_file())
        self.assertEqual(os.readlink(self.home / '.codex/AGENTS.md'), '../ai-memory/MEMORY.md')
        self.assertIn('# BEGIN LINUX TERMINAL AGENTS WORKBENCH', self.read('.bashrc'))
        self.assertTrue((self.home / f'.local/share/gnome-shell/extensions/{UUID}/schemas/gschemas.compiled').is_file())
        for name in ('.claude/settings.json', '.codex/config.toml'):
            self.assertEqual(stat.S_IMODE((self.home / name).stat().st_mode), 0o600, name)
        first = snapshot(self.home)
        out = self.cli('--install').stdout
        self.assertIn('nothing was changed', out)
        self.assertEqual(snapshot(self.home), first)
        self.assertEqual(len(self.backups()), 1)
        self.assertNothingTrapped()

    def test_only_adds_requirements(self):
        self.cli('--install', '--only', 'agents')
        self.assertTrue((self.home / '.local/bin/agent-pair').is_file())
        self.assertTrue((self.home / '.local/bin/workspace-control').is_file())    # workspace
        self.assertTrue((self.home / '.config/ghostty/config').is_file())         # terminal
        self.assertFalse((self.home / 'ai-memory').exists())
        self.assertFalse((self.home / '.local/share/gnome-shell').exists())

    def test_staged_paths_belong_to_the_target(self):
        values = resolve_values(self.home.resolve(), False)
        self.assertEqual(values['CLAUDE_BIN'], str(self.home.resolve() / '.local/bin/claude'))
        self.cli('--install', '--only', 'agents')
        self.assertIn(f'Exec="{self.home.resolve()}/.local/bin/claude" --handle-uri %u',
                      self.read('.local/share/applications/claude-code-url-handler.desktop'))

    def test_unknown_component(self):
        self.assertIn('Unknown component', self.cli('--check', '--only', 'nothing', ok=False).stderr)


class Safety(Scratch):
    def test_stage_refuses_a_folder_symlink_leading_outside(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (self.home / '.config').symlink_to(outside)
        with self.assertRaises(Unsafe):
            stage(self.home, ['terminal'])
        self.assertEqual(list(outside.iterdir()), [])

    def test_install_refuses_a_folder_symlink_leading_outside(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (self.home / '.config').symlink_to(outside)
        result = self.cli('--install', ok=False)
        self.assertIn('outside', result.stderr)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(self.backups(), [])

    def test_folder_symlink_inside_the_home_is_followed(self):
        (self.home / 'dotfiles/config').mkdir(parents=True)
        (self.home / '.config').symlink_to('dotfiles/config')
        self.cli('--install', '--only', 'terminal')
        self.assertTrue((self.home / 'dotfiles/config/ghostty/config').is_file())
        self.cli('--rollback', 'last')
        self.assertEqual(sorted(p.name for p in (self.home / 'dotfiles/config').iterdir()), [])

    def test_leaf_symlink_is_replaced_not_followed(self):
        outside = self.root / 'outside-config'
        outside.write_text('my own ghostty config\n')
        (self.home / '.config/ghostty').mkdir(parents=True)
        (self.home / '.config/ghostty/config').symlink_to(outside)
        out = self.cli('--install', '--only', 'terminal').stdout
        self.assertIn('replace the symlink', out)
        self.assertEqual(outside.read_text(), 'my own ghostty config\n')
        self.assertFalse((self.home / '.config/ghostty/config').is_symlink())
        self.cli('--rollback', 'last')
        self.assertEqual(os.readlink(self.home / '.config/ghostty/config'), str(outside))

    def test_rollback_refuses_a_folder_symlink_added_later(self):
        self.cli('--install', '--only', 'terminal')
        outside = self.root / 'outside'
        shutil.move(self.home / '.config', outside)
        (self.home / '.config').symlink_to(outside)
        before = snapshot(outside)
        result = self.cli('--rollback', 'last', ok=False)
        self.assertIn('outside', result.stdout + result.stderr)
        self.assertEqual(snapshot(outside), before)


class Preservation(Scratch):
    def populate(self):
        self.write('.bashrc', '# my aliases\nalias gs="git status"\n\n' + LEGACY_BLOCK + '\nexport MINE=1\n')
        self.write('.inputrc', 'set bell-style none\n')
        self.write('.claude/settings.json', json.dumps({
            'env': {'FOO': '1'}, 'model': 'sonnet', 'permissions': {'allow': ['Bash(ls)'], 'defaultMode': 'default'},
            'hooks': {'PreToolUse': [{'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': '/opt/my-hook'}]}]}},
            indent=2), 0o640)
        self.write('.codex/config.toml', '# my codex settings\nmodel = "my-model"\napproval_policy = "on-request"\n\n'
                   '[projects."/work/x"]\ntrust_level = "trusted"\n\n[mcp_servers.docs]\ncommand = "docs-server"\n', 0o644)
        self.write('.config/mimeapps.list', '[Default Applications]\ntext/plain=code.desktop;\n\n'
                   '[Added Associations]\ntext/plain=code.desktop;\n')
        self.write('.config/micro/settings.json', '{"tabsize": 2}\n')
        self.write('.gitconfig', '[core]\n\texcludesfile = ~/my-ignore\n')
        self.write('my-ignore', '*.log\n')
        self.write('.claude/CLAUDE.md', '# My rules\nBe brief.\n')
        self.write('.codex/AGENTS.md', '# My Codex rules\nAlways run tests.\n')
        self.write('.codex/AGENTS.override.md', '# Temporary override\nUse the staging server.\n')
        self.write('ai-memory/MEMORY.md', '# My memory\n\n## Index\n- [Fact](fact.md) — something\n')
        self.write('ai-memory/fact.md', 'a fact\n')
        self.write('ai-memory/projects/work/MEMORY.md', '- [Note](note.md) — n\n')

    def test_existing_settings_stay(self):
        self.populate()
        memory_before = {p: v for p, v in snapshot(self.home).items() if p.startswith('ai-memory')}
        self.cli('--install')
        bashrc = self.read('.bashrc')
        self.assertTrue(bashrc.startswith('# my aliases\nalias gs="git status"\n'))
        self.assertTrue(bashrc.rstrip().endswith('export MINE=1'))
        self.assertNotIn('an earlier version of this block', bashrc)
        self.assertEqual(bashrc.count('# BEGIN LINUX TERMINAL AGENTS WORKBENCH'), 1)
        self.assertIn('set bell-style none', self.read('.inputrc'))
        self.assertIn('"\\e[13;2u": accept-line', self.read('.inputrc'))
        claude = json.loads(self.read('.claude/settings.json'))
        self.assertEqual((claude['env'], claude['model'], claude['permissions']),
                         ({'FOO': '1'}, 'sonnet', {'allow': ['Bash(ls)'], 'defaultMode': 'default'}))
        self.assertEqual(claude['theme'], 'custom:amoled')
        pre = [h['command'] for g in claude['hooks']['PreToolUse'] for h in g['hooks']]
        self.assertEqual(pre, ['/opt/my-hook', f'{self.home.resolve()}/.local/bin/workspace-control hook'])
        self.assertEqual(stat.S_IMODE((self.home / '.claude/settings.json').stat().st_mode), 0o640)
        codex_text = self.read('.codex/config.toml')
        codex = tomllib.loads(codex_text)
        self.assertTrue(codex_text.startswith('suppress_unstable_features_warning = true\n'))
        self.assertIn('# my codex settings', codex_text)
        self.assertEqual((codex['model'], codex['approval_policy']), ('my-model', 'on-request'))
        self.assertEqual(codex['projects'], {'/work/x': {'trust_level': 'trusted'}})
        self.assertEqual(codex['mcp_servers'], {'docs': {'command': 'docs-server'}})
        self.assertEqual(codex['tui'], {'copy_on_select': 'always', 'right_click_paste': 'off'})
        self.assertEqual(stat.S_IMODE((self.home / '.codex/config.toml').stat().st_mode), 0o644)
        mime = self.read('.config/mimeapps.list')
        self.assertIn('text/plain=code.desktop;', mime)
        self.assertNotIn('text/plain=org.gnome.TextEditor.desktop;', mime)
        self.assertIn('text/markdown=md-view.desktop;', mime)
        self.assertIn('[Added Associations]\ntext/plain=code.desktop;', mime)
        self.assertEqual(json.loads(self.read('.config/micro/settings.json')), {'tabsize': 2, 'colorscheme': 'amoled'})
        self.assertEqual(self.read('my-ignore'), '*.log\n.ai-memory\n**/.claude/settings.local.json\n')
        self.assertFalse((self.home / '.config/git/ignore').exists())
        claude_md = self.read('.claude/CLAUDE.md')
        self.assertTrue(claude_md.startswith('# My rules\nBe brief.\n'))
        self.assertEqual(claude_md.count('@~/ai-memory/MEMORY.md'), 1)
        for name in ('.codex/AGENTS.md', '.codex/AGENTS.override.md'):
            text = self.read(name)
            self.assertFalse((self.home / name).is_symlink(), name)
            self.assertIn('read `~/ai-memory/MEMORY.md`', text)
        self.assertTrue(self.read('.codex/AGENTS.md').startswith('# My Codex rules\nAlways run tests.\n'))
        self.assertTrue(self.read('.codex/AGENTS.override.md').startswith('# Temporary override\n'))
        self.assertEqual({p: v for p, v in snapshot(self.home).items() if p.startswith('ai-memory')}, memory_before)
        installed = snapshot(self.home)
        self.assertIn('nothing was changed', self.cli('--install').stdout)
        self.assertEqual(snapshot(self.home), installed)

    def test_rollback_restores_the_exact_previous_state(self):
        self.populate()
        before = snapshot(self.home)
        self.cli('--install')
        self.cli('--rollback', 'last')
        self.assertEqual(snapshot(self.home), before)

    def test_rollback_refuses_after_a_later_edit(self):
        self.populate()
        self.cli('--install')
        (self.home / '.config/ghostty/config').write_text('edited later\n')
        installed = snapshot(self.home)
        result = self.cli('--rollback', 'last', ok=False)
        self.assertIn('.config/ghostty/config changed after installation', result.stderr)
        self.assertEqual(snapshot(self.home), installed)

    def test_rollback_keeps_new_memory(self):
        self.cli('--install', '--only', 'memory')
        index = self.home / 'ai-memory/MEMORY.md'
        index.write_text(index.read_text() + '- [Mine](mine.md) — written after installing\n')
        (self.home / 'ai-memory/mine.md').write_text('mine\n')
        out = self.cli('--rollback', 'last').stdout
        self.assertIn('ai-memory/MEMORY.md (changed since installing; it stays)', out)
        self.assertIn('written after installing', index.read_text())
        self.assertTrue((self.home / 'ai-memory/mine.md').exists())
        self.assertFalse((self.home / '.local/bin/ai-memory').exists())

    def test_agent_policy_is_opt_in(self):
        self.cli('--install', '--only', 'agents')
        self.assertNotIn('permissions', json.loads(self.read('.claude/settings.json')))
        self.assertNotIn('approval_policy', tomllib.loads(self.read('.codex/config.toml')))
        self.cli('--install', '--only', 'agents', '--agent-policy', 'full-access')
        claude = json.loads(self.read('.claude/settings.json'))
        codex = tomllib.loads(self.read('.codex/config.toml'))
        self.assertEqual(claude['permissions'], {'defaultMode': 'bypassPermissions'})
        self.assertEqual((codex['approval_policy'], codex['sandbox_mode']), ('never', 'danger-full-access'))
        self.assertTrue(codex['notice']['hide_full_access_warning'])

    def test_codex_home_is_honoured_on_a_real_install(self):
        context = Context(home=Home(self.home), values=resolve_values(self.home.resolve(), False), real=True,
                          env={'CODEX_HOME': str(self.home / '.config/codex')})
        targets = {c.path for c in build(load(), context, ['agents', 'memory']).changes}
        self.assertIn('.config/codex/config.toml', targets)
        self.assertIn('.config/codex/AGENTS.md', targets)
        self.assertNotIn('.codex/config.toml', targets)


class Recovery(Scratch):
    def test_failure_midway_undoes_everything(self):
        Preservation.populate(self)
        before = snapshot(self.home)
        original = transaction.Transaction._do

        def failing(self_, step):
            if step.get('path') == '.codex/config.toml':
                raise OSError('disk full (simulated)')
            original(self_, step)

        with mock.patch.object(transaction.Transaction, '_do', failing), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(OSError):
            Run(Options(home=self.home, real=False)).install()
        self.assertEqual(snapshot(self.home), before)
        journal = json.loads((self.backups()[-1] / 'journal.json').read_text())
        self.assertEqual(journal['status'], 'rolled-back')
        self.assertIn('disk full', journal['error'])
        self.cli('--install')

    def test_interrupted_run_must_be_rolled_back_first(self):
        Preservation.populate(self)
        before = snapshot(self.home)
        script = f'''
import os, sys
sys.path.insert(0, {str(REPO)!r})
from pathlib import Path
from installer import transaction
from installer.engine import Options, Run
original, count = transaction.Transaction._do, [0]
def stop_after(self, step):
    original(self, step)
    count[0] += 1
    if count[0] == 60:
        os._exit(9)   # killed after a step finished but before the journal recorded it
transaction.Transaction._do = stop_after
Run(Options(home=Path({str(self.home)!r}), real=False), log=lambda *a: None).install()
'''
        result = subprocess.run([sys.executable, '-c', script], env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 9, result.stderr)
        self.assertNotEqual(snapshot(self.home), before)
        refused = self.cli('--install', ok=False)
        self.assertIn('did not finish', refused.stderr)
        self.cli('--rollback', 'last')
        self.assertEqual(snapshot(self.home), before)
        self.cli('--install')

    def test_corrupt_backup_is_refused(self):
        Preservation.populate(self)
        self.cli('--install')
        blob = next((self.backups()[-1] / 'files').iterdir())
        blob.write_bytes(b'damaged')
        installed = snapshot(self.home)
        result = self.cli('--rollback', 'last', ok=False)
        self.assertIn('damaged', result.stderr)
        self.assertEqual(snapshot(self.home), installed)


FAKE_GSETTINGS = '''#!/usr/bin/env python3
import json, os, sys
path = os.environ['FAKE_GSETTINGS']
state = json.load(open(path))
op, schema, key = sys.argv[1:4]
if schema + ' ' + key not in state:
    sys.exit(1)
if op == 'get':
    print(state[schema + ' ' + key])
elif op == 'set':
    state[schema + ' ' + key] = sys.argv[4]
    json.dump(state, open(path, 'w'))
'''
FAKE_SYSTEMCTL = '''#!/usr/bin/env python3
import json, os, sys
path = os.environ['FAKE_SYSTEMCTL']
state = json.load(open(path))
args = [a for a in sys.argv[1:] if a != '--user']
state.setdefault('log', []).append(' '.join(args))
units = state.setdefault('units', {})
if args[0] in ('is-enabled', 'is-active'):
    unit = units.get(args[1], {'enabled': 'disabled', 'active': 'inactive'})
    value = unit['enabled' if args[0] == 'is-enabled' else 'active']
    print(value)
    json.dump(state, open(path, 'w'))
    sys.exit(0 if value in ('enabled', 'active') else 1)
if args[0] in ('enable', 'disable', 'start', 'stop', 'restart'):
    unit = units.setdefault(args[1], {'enabled': 'disabled', 'active': 'inactive'})
    if args[0] in ('enable', 'disable'):
        unit['enabled'] = args[0] + 'd'
    else:
        unit['active'] = 'inactive' if args[0] == 'stop' else 'active'
json.dump(state, open(path, 'w'))
'''
INITIAL_SETTINGS = {
    'org.gnome.desktop.default-applications.terminal exec': "'gnome-terminal'",
    'org.gnome.desktop.default-applications.terminal exec-arg': "''",
    'org.gnome.settings-daemon.plugins.media-keys terminal': "['<Primary><Alt>t']",
    'org.gnome.shell favorite-apps': "['firefox.desktop']",
    'org.gnome.shell enabled-extensions': "['ubuntu-dock@ubuntu.com', 'old-clipboard@example']",
    'org.gnome.shell disabled-extensions': '@as []',
    'org.gnome.shell.keybindings toggle-message-tray': "['<Super>v', '<Super>m']",
    'org.gnome.desktop.interface color-scheme': "'default'",
    'org.gnome.desktop.interface gtk-theme': "'Yaru'",
    'org.gnome.desktop.interface icon-theme': "'Yaru'",
    'org.gnome.desktop.interface accent-color': "'orange'",
}


class DesktopIntegration(Scratch):
    def setUp(self):
        super().setUp()
        fakes = self.root / 'fakes'
        fakes.mkdir()
        for name, body in (('gsettings', FAKE_GSETTINGS), ('systemctl', FAKE_SYSTEMCTL)):
            (fakes / name).write_text(body)
            (fakes / name).chmod(0o755)
        self.settings = self.root / 'gsettings.json'
        self.settings.write_text(json.dumps(INITIAL_SETTINGS))
        self.units = self.root / 'systemctl.json'
        self.units.write_text('{}')
        env = dict(os.environ, PATH=f'{fakes}:{os.environ["PATH"]}', FAKE_GSETTINGS=str(self.settings),
                   FAKE_SYSTEMCTL=str(self.units))
        self.desktop = Desktop(env=env)
        legacy = self.home / '.local/share/gnome-shell/extensions/old-clipboard@example'
        legacy.mkdir(parents=True)
        (legacy / 'metadata.json').write_text(json.dumps({'uuid': 'old-clipboard@example',
                                                          'settings-schema': 'org.gnome.shell.extensions.clipboard-list'}))

    def run_install(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            Run(Options(home=self.home, real=False, desktop=True), desktop=self.desktop).install()
        return out.getvalue()

    def value(self, key):
        return json.loads(self.settings.read_text())[key]

    def test_settings_units_and_rollback(self):
        out = self.run_install()
        self.assertIn('dash-to-dock dock-position: not on this desktop, skipped', out)
        self.assertEqual(self.value('org.gnome.shell enabled-extensions'), f"['ubuntu-dock@ubuntu.com', '{UUID}']")
        self.assertEqual(self.value('org.gnome.shell.keybindings toggle-message-tray'), "['<Super>m']")
        self.assertEqual(self.value('org.gnome.shell favorite-apps'), "['firefox.desktop', 'workspace.desktop']")
        self.assertEqual(self.value('org.gnome.settings-daemon.plugins.media-keys terminal'),
                         "['<Primary><Alt>t', '<Super>t']")
        self.assertEqual(self.value('org.gnome.desktop.interface color-scheme'), "'prefer-dark'")
        units = json.loads(self.units.read_text())['units']
        for unit in ('workspace-monitor.service', 'workspace-save.timer', 'ai-memory-snapshot.timer'):
            self.assertEqual(units[unit], {'enabled': 'enabled', 'active': 'active'}, unit)
        with contextlib.redirect_stdout(io.StringIO()):
            rollback(Options(home=self.home, real=False), 'last', desktop=self.desktop)
        self.assertEqual(json.loads(self.settings.read_text()), INITIAL_SETTINGS)
        units = json.loads(self.units.read_text())['units']
        for unit in ('workspace-monitor.service', 'workspace-save.timer', 'ai-memory-snapshot.timer'):
            self.assertEqual(units[unit], {'enabled': 'disabled', 'active': 'inactive'}, unit)

    def test_rollback_refuses_a_setting_changed_later(self):
        self.run_install()
        state = json.loads(self.settings.read_text())
        state['org.gnome.desktop.interface accent-color'] = "'green'"
        self.settings.write_text(json.dumps(state))
        with self.assertRaises(transaction.RollbackRefused) as refused:
            rollback(Options(home=self.home, real=False), 'last', desktop=self.desktop)
        self.assertIn('accent-color', str(refused.exception))
        self.assertEqual(self.value('org.gnome.shell.keybindings toggle-message-tray'), "['<Super>m']")

    def test_repeat_changes_nothing(self):
        self.run_install()
        out = self.run_install()
        self.assertIn('nothing was changed', out)


class Platform(unittest.TestCase):
    SUPPORTED = load().supported
    GOOD = {'python': (3, 14), 'uid': 1000, 'os': 'ubuntu', 'version': '26.04', 'architecture': 'amd64',
            'desktop': 'ubuntu:GNOME', 'gnome': '50', 'session': 'wayland', 'systemd': True}

    def test_tested_target_passes(self):
        self.assertEqual(preflight.problems(self.SUPPORTED, self.GOOD), [])

    def test_other_systems_are_refused_with_reasons(self):
        found = dict(self.GOOD, os='fedora', version='42', session='x11', gnome='48', uid=0, architecture='arm64')
        reasons = '\n'.join(preflight.problems(self.SUPPORTED, found))
        for words in ('Ubuntu 26.04', 'Wayland', 'GNOME Shell 48', 'not with sudo', 'arm64'):
            self.assertIn(words, reasons)


class AgentCLIs(unittest.TestCase):
    CLAUDE = '''#!/bin/bash
[ "$1" = "stable" ] || { echo "unexpected release $1" >&2; exit 3; }
mkdir -p "$HOME/.local/bin"
printf '#!/bin/sh\\necho "9.9.9 (Claude Code)"\\n' > "$HOME/.local/bin/claude"
chmod +x "$HOME/.local/bin/claude"
'''
    CODEX = '''#!/bin/sh
[ "$1" = "--release" ] && [ "$2" = "1.2.3" ] && [ "$CODEX_NON_INTERACTIVE" = "1" ] || exit 3
mkdir -p "$HOME/.local/bin"
printf '#!/bin/sh\\necho "codex-cli 1.2.3"\\n' > "$HOME/.local/bin/codex"
chmod +x "$HOME/.local/bin/codex"
'''

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='workbench-agents-'))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        site = self.root / 'site'
        for name, body in (('claude', self.CLAUDE), ('codex', self.CODEX)):
            (site / name).mkdir(parents=True)
            (site / name / 'install.sh').write_text(body)
        handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=str(site), **k)  # noqa: E731
        http.server.SimpleHTTPRequestHandler.log_message = lambda *a: None
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f'http://127.0.0.1:{self.server.server_address[1]}'
        self.home = self.root / 'home'
        self.home.mkdir()
        self.downloads = self.root / 'downloads'
        self.downloads.mkdir()

    def agent(self, name, version):
        spec = load().components['agents']['cli'][name]
        return packages.Agent(name, f'{self.base}/{name}/install.sh', spec['interpreter'], spec['args'],
                              spec.get('env', {}), version, spec['versions'], spec['docs'])

    def test_vendor_installers_run_for_the_requested_release(self):
        with contextlib.redirect_stdout(io.StringIO()):
            claude = packages.install_agent(self.agent('claude', 'stable'), self.home, self.downloads)
            codex = packages.install_agent(self.agent('codex', '1.2.3'), self.home, self.downloads)
        self.assertEqual(claude, (str(self.home / '.local/bin/claude'), '9.9.9 (Claude Code)'))
        self.assertEqual(codex, (str(self.home / '.local/bin/codex'), 'codex-cli 1.2.3'))
        kept = sorted(p.name for p in self.downloads.glob('*/*'))
        self.assertEqual(kept, ['SHA256SUMS', 'SHA256SUMS', 'install.sh', 'install.sh'])

    def test_a_failing_installer_is_reported(self):
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
            packages.install_agent(self.agent('claude', 'latest'), self.home, self.downloads)

    def test_only_https_or_a_local_test_server(self):
        with self.assertRaises(ValueError):
            packages.download('http://example.com/install.sh', self.downloads)

    def test_existing_clis_are_kept(self):
        bin_dir = self.home / '.local/bin'
        bin_dir.mkdir(parents=True)
        for name, version in (('claude', '1.0.0 (Claude Code)'), ('codex', 'codex-cli 0.1.0')):
            (bin_dir / name).write_text(f'#!/bin/sh\necho "{version}"\n')
            (bin_dir / name).chmod(0o755)
        run = Run(Options(home=self.home, real=False, agents=True))
        with mock.patch.object(packages, 'download', side_effect=AssertionError('downloaded')):
            versions = run.install_agents()
        self.assertEqual({n: (v['version'], v['installed']) for n, v in versions.items()},
                         {'claude': ('1.0.0 (Claude Code)', False), 'codex': ('codex-cli 0.1.0', False)})

    def test_release_names_are_checked(self):
        with self.assertRaises(ValueError):
            packages.agents_from(load().components['agents'], {'codex': 'stable; rm -rf ~'})


if __name__ == '__main__':
    unittest.main(verbosity=2)
