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
import pwd
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
from installer import merges, packages, preflight, transaction  # noqa: E402
from installer import cli as cli_module  # noqa: E402
from installer import plan as plan_module  # noqa: E402
from installer.desktop import Desktop  # noqa: E402
from installer.engine import Options, Refused, Run, resolve_values, rollback  # noqa: E402
from installer.fsops import Home, Unsafe  # noqa: E402
from installer.manifest import load  # noqa: E402
from installer.plan import Context, Plan, build  # noqa: E402
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
    def broken_agents(self):
        binaries = {}
        for name in ('claude', 'codex'):
            marker = self.root / f'{name}-executed'
            body = f'#!/bin/sh\n: > "{marker}"\nexit 139\n'
            binaries[name] = (self.write(f'.local/bin/{name}', body, 0o755), marker, body)
        return binaries

    def assertAgentsNotRun(self, binaries):
        for binary, marker, body in binaries.values():
            self.assertFalse(marker.exists(), f'{binary} was executed')
            self.assertEqual(binary.read_text(), body)
        self.assertTrue((self.home / '.local/bin/workspace-control').is_file())
        journal = json.loads((self.backups()[-1] / 'journal.json').read_text())
        for name, (binary, _, _) in binaries.items():
            self.assertEqual(journal['agents'][name],
                             {'path': str(binary), 'version': None, 'installed': False})
        self.assertNothingTrapped()

    def test_staging_does_not_execute_existing_agent_binaries(self):
        binaries = self.broken_agents()
        self.cli('--check')
        self.assertIn('present; not run (files only)', self.cli('--install').stdout)
        self.assertAgentsNotRun(binaries)
        self.cli('--install')   # repeated staging must not execute them either
        self.assertAgentsNotRun(binaries)

    def test_no_activate_does_not_execute_existing_agent_binaries(self):
        binaries = self.broken_agents()
        # Exercise the real CLI's --no-activate path, but all writes still go to the scratch home.
        with mock.patch.object(pwd, 'getpwuid', return_value=mock.Mock(pw_dir=str(self.home))), \
                mock.patch.object(Run, 'platform_problems', return_value=[]), \
                mock.patch.object(Desktop, 'unit_state', return_value={'enabled': 'not-found', 'active': 'inactive'}), \
                mock.patch.dict(os.environ, dict(self.env, HOME=str(self.home))), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli_module.main(['--install', '--no-activate']), 0)
        self.assertIn('present; not run (files only)', out.getvalue())
        self.assertAgentsNotRun(binaries)
        record = json.loads(self.read('.local/state/workbench/installed.json'))
        self.assertTrue(all(info['version'] is None for info in record['agents'].values()))

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

    def test_target_home_never_means_the_real_home(self):
        account = Path(pwd.getpwuid(os.getuid()).pw_dir)
        for target in (account, account.parent):
            result = subprocess.run([INSTALL, '--check', '--target-home', str(target)], env=self.env,
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn('your own home or contains it', result.stderr)
        with self.assertRaises(Refused):
            Run(Options(home=account, real=False))
        self.assertNothingTrapped()

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

    def test_links_in_symlinked_folders_reach_their_targets(self):
        (self.home / 'dotfiles/codex').mkdir(parents=True)
        (self.home / 'dotfiles/yazi-sidebar').mkdir(parents=True)
        (self.home / '.codex').symlink_to('dotfiles/codex')
        (self.home / '.config').mkdir()
        (self.home / '.config/yazi-sidebar').symlink_to('../dotfiles/yazi-sidebar')
        self.cli('--install')
        home = self.home.resolve()
        self.assertEqual((self.home / '.codex/AGENTS.md').resolve(), home / 'ai-memory/MEMORY.md')
        self.assertEqual((self.home / '.config/yazi-sidebar/theme.toml').resolve(), home / '.config/yazi/theme.toml')
        installed = snapshot(self.home)
        self.assertIn('nothing was changed', self.cli('--install').stdout)
        self.assertEqual(snapshot(self.home), installed)
        self.cli('--rollback', 'last')
        self.assertEqual(sorted(p.name for p in (self.home / 'dotfiles/codex').iterdir()), [])

    def test_staged_links_in_symlinked_folders_reach_their_targets(self):
        (self.home / 'dotfiles/yazi-sidebar').mkdir(parents=True)
        (self.home / '.config').mkdir()
        (self.home / '.config/yazi-sidebar').symlink_to('../dotfiles/yazi-sidebar')
        stage(self.home, ['workspace'])
        self.assertEqual((self.home / '.config/yazi-sidebar/theme.toml').resolve(),
                         self.home.resolve() / '.config/yazi/theme.toml')

    def test_planning_works_without_git_and_reads_its_config(self):
        bin_dir = self.root / 'no-git'
        bin_dir.mkdir()
        for tool in ('python3', 'sh', 'dirname', 'glib-compile-schemas'):
            (bin_dir / tool).symlink_to(shutil.which(tool))
        self.write('.gitconfig', '[user]\n\tname = x\n[core]\n\texcludesFile = "~/my-ignore" ; ours\n')
        result = subprocess.run([INSTALL, '--install', '--only', 'memory', '--target-home', str(self.home)],
                                env=dict(self.env, PATH=str(bin_dir)), capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('git is not installed yet', result.stdout)
        self.assertEqual(self.read('my-ignore'), '.ai-memory\n**/.claude/settings.local.json\n')
        self.assertFalse((self.home / '.config/git/ignore').exists())
        self.assertIn('nothing was changed', self.cli('--install', '--only', 'memory').stdout)   # git agrees

    def test_a_user_masked_unit_stays_masked(self):
        units = self.home / '.config/systemd/user'
        units.mkdir(parents=True)
        (units / 'workspace-save.timer').symlink_to(os.devnull)
        out = self.cli('--install', '--only', 'workspace').stdout
        self.assertIn('workspace-save.timer is masked', out)
        self.assertEqual(os.readlink(units / 'workspace-save.timer'), os.devnull)
        self.assertTrue((units / 'workspace-save.service').is_file())
        self.assertIn('nothing was changed', self.cli('--install', '--only', 'workspace').stdout)

    def test_a_system_mask_is_not_overridden(self):
        system = self.root / 'etc-systemd-user'
        system.mkdir()
        (system / 'workspace-monitor.service').symlink_to(os.devnull)
        context = Context(home=Home(self.home), values=resolve_values(self.home.resolve(), False), real=True, env={})
        with mock.patch.object(plan_module, 'SYSTEM_UNIT_DIRS', (system,)):
            plan = build(load(), context, ['workspace'])
        self.assertNotIn('.config/systemd/user/workspace-monitor.service', {c.path for c in plan.changes})
        self.assertEqual(plan.masked, {'workspace-monitor.service'})
        self.assertTrue(any(f'masked in {system}' in note for note in plan.notes), plan.notes)

    def test_a_runtime_mask_file_is_not_overridden(self):
        runtime = self.root / 'run-user'
        (runtime / 'systemd/user').mkdir(parents=True)
        (runtime / 'systemd/user/workspace-monitor.service').symlink_to(os.devnull)
        context = Context(home=Home(self.home), values=resolve_values(self.home.resolve(), False), real=True,
                          env={'XDG_RUNTIME_DIR': str(runtime)})
        with mock.patch.object(plan_module, 'SYSTEM_UNIT_DIRS', ()):
            plan = build(load(), context, ['workspace'])
        self.assertNotIn('.config/systemd/user/workspace-monitor.service', {c.path for c in plan.changes})
        self.assertEqual(plan.masked, {'workspace-monitor.service'})

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

    def test_a_conflict_during_automatic_undo_is_reported_as_failed(self):
        Preservation.populate(self)
        before = snapshot(self.home)
        original, calls, edited = transaction.Transaction._do, [], {}

        def edit_then_fail(self_, step):
            if calls and step['type'] == 'path':
                # Someone edits a file the installation already replaced, then the next step fails.
                path = edited.setdefault('path', calls[0])
                (self.home / path).write_text('edited during the installation\n')
                raise RuntimeError('the next step failed (simulated)')
            original(self_, step)
            if step['type'] == 'path' and step['after']['kind'] == 'file':
                calls.append(step['path'])

        with mock.patch.object(transaction.Transaction, '_do', edit_then_fail), \
                contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaises(RuntimeError):
            Run(Options(home=self.home, real=False)).install()
        report = out.getvalue()
        self.assertIn('could not undo everything', report)
        self.assertNotIn('was put back', report)
        self.assertIn(f'not undone: ~/{edited["path"]} changed after installation', report)
        journal = json.loads((self.backups()[-1] / 'journal.json').read_text())
        self.assertEqual(journal['status'], 'failed')
        self.assertEqual(journal['recovery']['problems'], [f'~/{edited["path"]} changed after installation'])
        self.assertIn('did not finish', self.cli('--install', ok=False).stderr)
        self.assertIn('changed after installation', self.cli('--rollback', 'last', ok=False).stderr)
        self.cli('--rollback', 'last', '--keep-changed')
        now = snapshot(self.home)
        self.assertEqual(now[edited['path']][2], b'edited during the installation\n')
        kept = {edited['path'], *(str(p) for p in Path(edited['path']).parents if str(p) != '.')}
        self.assertEqual({k: v for k, v in now.items() if k not in kept},
                         {k: v for k, v in before.items() if k not in kept})
        self.cli('--install')

    def test_the_file_plan_is_checked_before_packages_and_agents(self):
        self.write('.claude/settings.json', '{broken')
        calls = []
        with mock.patch.object(packages, 'missing_apt', side_effect=lambda names: calls.append('apt') or []), \
                mock.patch.object(packages, 'missing_snaps', side_effect=lambda *a: calls.append('snap') or []), \
                mock.patch.object(packages, 'install_agent', side_effect=lambda *a, **k: calls.append('agent')), \
                self.assertRaises(Refused) as refused:
            Run(Options(home=self.home, real=False, packages=True, agents=True), log=lambda *a: None).install()
        self.assertIn('not valid JSON', str(refused.exception))
        self.assertEqual(calls, [])
        (self.home / '.claude/settings.json').unlink()
        outside = self.root / 'outside'
        outside.mkdir()
        (self.home / '.config').symlink_to(outside)
        with mock.patch.object(packages, 'missing_apt', side_effect=lambda names: calls.append('apt') or []), \
                self.assertRaises(Refused):
            Run(Options(home=self.home, real=False, packages=True), log=lambda *a: None).install()
        self.assertEqual(calls, [])

    def test_an_interrupted_rollback_is_finished_by_the_next_one(self):
        Preservation.populate(self)
        before = snapshot(self.home)
        self.cli('--install')
        script = f'''
import os, sys
sys.path.insert(0, {str(REPO)!r})
from pathlib import Path
from installer import transaction
from installer.engine import Options, rollback
original = transaction.restore
def stop_partway(*args, progress=None, **kwargs):
    count = [0]
    def tracked(item):
        progress(item)
        count[0] += 1
        if count[0] == 20:
            os._exit(9)     # killed in the middle of the rollback
    return original(*args, progress=tracked, **kwargs)
transaction.restore = stop_partway
rollback(Options(home=Path({str(self.home)!r}), real=False), 'last', log=lambda *a: None)
'''
        result = subprocess.run([sys.executable, '-c', script], env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 9, result.stderr)
        journal = json.loads((self.backups()[-1] / 'journal.json').read_text())
        self.assertEqual((journal['status'], len(journal['rollbacks'][-1]['restored'])), ('rolling-back', 20))
        self.assertIn('installation or rollback did not finish', self.cli('--install', ok=False).stderr)
        self.cli('--rollback', 'last')
        self.assertEqual(snapshot(self.home), before)
        self.assertEqual(json.loads((self.backups()[-1] / 'journal.json').read_text())['status'], 'rolled-back')

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
runtime = '--runtime' in args
args = [a for a in args if a != '--runtime']
units = state.setdefault('units', {})
# Like systemd: a unit with no unit file anywhere is "not-found"; a unit file linked to /dev/null is masked.
# Units seeded by a test stand for unit files elsewhere; units first touched here need their home file.
folder = os.environ.get('FAKE_UNIT_DIR', '')
def unit_file(name):
    return os.path.join(folder, name) if folder else ''
def has_file(name):
    return bool(folder) and os.path.lexists(unit_file(name)) and os.path.realpath(unit_file(name)) != os.devnull
if args[0] in ('is-enabled', 'is-active'):
    name, unit = args[1], units.get(args[1])
    masked = bool(folder) and os.path.islink(unit_file(name)) and os.path.realpath(unit_file(name)) == os.devnull
    found = not (unit is None and not has_file(name) or unit is not None and unit.get('home_file') and not has_file(name))
    if args[0] == 'is-enabled':
        value = 'masked' if masked else (unit or {}).get('enabled', 'disabled') if found else 'not-found'
    else:
        value = (unit or {}).get('active', 'inactive') if found else 'inactive'
    print(value)
    json.dump(state, open(path, 'w'))
    sys.exit(0 if value in ('enabled', 'enabled-runtime', 'static', 'active') else 4 if not found else 1)
if args[0] in ('enable', 'start', 'restart') and args[1] not in units and not has_file(args[1]):
    print(f'Unit file {args[1]} does not exist.', file=sys.stderr)
    sys.exit(1)
if ' '.join(args) in state.get('fail', []):
    if args[0] in ('restart', 'try-restart', 'start'):
        units.setdefault(args[1], {})['active'] = 'failed'
    json.dump(state, open(path, 'w'))
    sys.exit(1)
if args[0] in ('restart', 'try-restart', 'start') and os.path.exists(os.environ.get('FAKE_WATCH', '')):
    state.setdefault('seen', []).append(open(os.environ['FAKE_WATCH']).read())   # what the service would run
if args[0] == 'try-restart':
    args[0] = 'restart' if units.get(args[1], {}).get('active') == 'active' else 'noop'
if args[0] in ('enable', 'disable', 'start', 'stop', 'restart'):
    if args[1] not in units:     # first known through its unit file in the home
        units[args[1]] = {'enabled': 'disabled', 'active': 'inactive', 'home_file': has_file(args[1])}
    unit = units[args[1]]
    if args[0] in ('enable', 'disable'):
        unit['enabled'] = 'enabled-runtime' if args[0] == 'enable' and runtime else args[0] + 'd'
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
        self.fake_env = dict(os.environ, PATH=f'{fakes}:{os.environ["PATH"]}', FAKE_GSETTINGS=str(self.settings),
                             FAKE_SYSTEMCTL=str(self.units), FAKE_WATCH=str(self.home / '.local/share/workspace/workspace.py'),
                             FAKE_UNIT_DIR=str(self.home / '.config/systemd/user'))
        self.desktop = Desktop(env=self.fake_env)
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
        for unit in ('workspace-monitor.service', 'workspace-save.timer', 'ai-memory-snapshot.timer'):
            self.assertEqual(self.unit(unit), {'enabled': 'enabled', 'active': 'active'}, unit)
        with contextlib.redirect_stdout(io.StringIO()):
            rollback(Options(home=self.home, real=False), 'last', desktop=self.desktop)
        self.assertEqual(json.loads(self.settings.read_text()), INITIAL_SETTINGS)
        for unit in ('workspace-monitor.service', 'workspace-save.timer', 'ai-memory-snapshot.timer'):
            self.assertEqual(self.unit(unit), {'enabled': 'disabled', 'active': 'inactive'}, unit)
            # Its unit file is gone again, so systemd no longer knows it, as before the installation.
            self.assertEqual(self.systemd(unit), {'enabled': 'not-found', 'active': 'inactive'}, unit)

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

    def test_updated_active_timer_is_reloaded_and_restarted(self):
        self.run_install()
        timer = self.home / '.config/systemd/user/workspace-save.timer'
        timer.write_text(timer.read_text().replace('10min', '2min'))
        state = json.loads(self.units.read_text())
        previous = len(state['log'])
        # A new invocation has its own daemon-reload tracking.
        self.desktop = Desktop(env=self.fake_env)
        self.run_install()
        after = json.loads(self.units.read_text())['log'][previous:]
        self.assertIn('restart workspace-save.timer', after)
        self.assertLess(after.index('daemon-reload'), after.index('restart workspace-save.timer'))
        self.assertIn('10min', timer.read_text())

    def unit(self, name):
        unit = json.loads(self.units.read_text()).get('units', {}).get(name)
        return None if unit is None else {k: v for k, v in unit.items() if k != 'home_file'}

    def unit_file(self, name):
        path = self.home / '.config/systemd/user' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('[Unit]\nDescription=test\n')

    def systemd(self, name):
        """What systemctl reports for a unit now (enablement, activity)."""
        return self.desktop.unit_state(name)

    def test_a_setting_changed_by_a_failing_step_is_undone(self):
        class Flaky(Desktop):
            failed = False

            def set_value(self, schema, key, value):
                super().set_value(schema, key, value)
                if not self.failed:
                    self.failed = True
                    raise OSError('gsettings crashed after writing (simulated)')
        desktop = Flaky(env=self.fake_env)
        steps, _ = desktop.plan_settings([('set', 'org.gnome.desktop.interface', 'accent-color', "'blue'")])
        tx = transaction.Transaction(Home(self.home), Plan(home=self.home, components=[]), desktop)
        tx.prepare(steps, [])
        with self.assertRaises(OSError):
            tx.apply()
        self.assertEqual(self.value('org.gnome.desktop.interface accent-color'), "'orange'")
        self.assertEqual(tx.journal['status'], 'rolled-back')

    def test_a_unit_enabled_by_a_failing_step_is_undone(self):
        class Flaky(Desktop):
            def set_unit(self, unit, after):
                self.command('systemctl', '--user', 'enable', unit, check=True)
                raise OSError('the service failed to start (simulated)')
        desktop = Flaky(env=self.fake_env)
        self.unit_file('workspace-save.timer')
        steps = desktop.plan_units(['workspace-save.timer'])
        tx = transaction.Transaction(Home(self.home), Plan(home=self.home, components=[]), desktop)
        tx.prepare([], steps)
        with self.assertRaises(OSError):
            tx.apply()
        self.assertEqual(self.unit('workspace-save.timer'), {'enabled': 'disabled', 'active': 'inactive'})
        self.assertEqual(tx.journal['status'], 'rolled-back')

    def test_a_failed_service_undo_is_not_reported_as_rolled_back(self):
        class Flaky(Desktop):
            def set_unit(self, unit, after):
                self.command('systemctl', '--user', 'enable', unit, check=True)
                raise OSError('start failed (simulated)')

            def command(self, *args, check=False):
                if 'disable' in args:
                    result = subprocess.CompletedProcess(args, 1, '', 'disable failed')
                    if check:
                        result.check_returncode()
                    return result
                return super().command(*args, check=check)

        desktop = Flaky(env=self.fake_env)
        self.unit_file('workspace-save.timer')
        tx = transaction.Transaction(Home(self.home), Plan(home=self.home, components=[]), desktop)
        tx.prepare([], desktop.plan_units(['workspace-save.timer']))
        with self.assertRaises(OSError):
            tx.apply()
        self.assertEqual(tx.journal['status'], 'failed')
        self.assertIn('disable', tx.journal['recovery']['undo_error'])
        self.assertEqual(self.unit('workspace-save.timer')['enabled'], 'enabled')

    def test_services_restart_on_the_restored_files(self):
        old = self.home / '.local/share/workspace/workspace.py'
        old.parent.mkdir(parents=True)
        old.write_text('# the previous workspace code\n')
        self.units.write_text(json.dumps({'units': {'workspace-monitor.service': {'enabled': 'enabled', 'active': 'active'}}}))
        self.run_install()
        state = json.loads(self.units.read_text())
        self.assertNotEqual(state['seen'][-1], '# the previous workspace code\n')   # restarted on the new code
        installed_log = len(state['log'])
        with contextlib.redirect_stdout(io.StringIO()):
            rollback(Options(home=self.home, real=False), 'last', desktop=self.desktop)
        state = json.loads(self.units.read_text())
        after = state['log'][installed_log:]
        self.assertEqual(state['seen'][-1], '# the previous workspace code\n')        # restarted after the files
        self.assertLess(after.index('daemon-reload'), after.index('try-restart workspace-monitor.service'))
        self.assertLess(after.index('stop workspace-save.timer'), after.index('daemon-reload'))
        self.assertEqual(self.unit('workspace-monitor.service'), {'enabled': 'enabled', 'active': 'active'})

    def test_failed_restart_recovers_a_previously_running_service(self):
        unit = 'workspace-monitor.service'
        self.units.write_text(json.dumps({'units': {unit: {'enabled': 'enabled', 'active': 'active'}}}))

        class Flaky(Desktop):
            def set_unit(self, unit, after):
                self.command('systemctl', '--user', 'stop', unit, check=True)
                raise OSError('restart failed after stopping (simulated)')

        desktop = Flaky(env=self.fake_env)
        tx = transaction.Transaction(Home(self.home), Plan(home=self.home, components=[]), desktop)
        tx.prepare([], desktop.plan_units([unit], restart=[unit]))
        with self.assertRaises(OSError):
            tx.apply()
        self.assertEqual(self.unit(unit), {'enabled': 'enabled', 'active': 'active'})
        self.assertEqual(tx.journal['status'], 'rolled-back')

    def test_runtime_enablement_is_restored_after_files(self):
        unit = 'workspace-save.timer'
        before = {'enabled': 'enabled-runtime', 'active': 'active'}
        self.units.write_text(json.dumps({'units': {unit: before}}))
        tx = transaction.Transaction(Home(self.home), Plan(home=self.home, components=[]), self.desktop)
        folder = tx.prepare([], self.desktop.plan_units([unit]))
        tx.apply()
        self.assertEqual(self.unit(unit)['enabled'], 'enabled')
        transaction.rollback(Home(self.home), folder, self.desktop)
        self.assertEqual(self.unit(unit), before)

    def test_a_failed_rollback_is_journaled_and_can_be_retried(self):
        old = self.home / '.local/share/workspace/workspace.py'
        old.parent.mkdir(parents=True)
        old.write_text('# the previous workspace code\n')
        state = {'units': {'workspace-monitor.service': {'enabled': 'enabled', 'active': 'active'}}}
        self.units.write_text(json.dumps(state))
        self.run_install()
        state = json.loads(self.units.read_text())
        state['fail'] = ['try-restart workspace-monitor.service']
        self.units.write_text(json.dumps(state))
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(transaction.RollbackIncomplete) as stopped:
            rollback(Options(home=self.home, real=False), 'last', desktop=self.desktop)
        self.assertIn('~/.local/share/workspace/workspace.py', stopped.exception.restored)
        journal = json.loads((self.backups()[-1] / 'journal.json').read_text())
        self.assertEqual(journal['status'], 'rollback-failed')
        self.assertIn('try-restart', journal['rollbacks'][-1]['error'])
        self.assertIn('~/.local/share/workspace/workspace.py', journal['rollbacks'][-1]['restored'])
        self.assertEqual(journal['applied'], 'complete')
        self.assertTrue(transaction.unfinished(Home(self.home)))
        with self.assertRaises(Refused):
            self.run_install()
        self.assertEqual(old.read_text(), '# the previous workspace code\n')
        state = json.loads(self.units.read_text())
        state['fail'] = []
        self.units.write_text(json.dumps(state))
        with contextlib.redirect_stdout(io.StringIO()):
            rollback(Options(home=self.home, real=False), 'last', desktop=self.desktop)
        self.assertEqual(self.unit('workspace-monitor.service'), {'enabled': 'enabled', 'active': 'active'})
        self.assertEqual(json.loads(self.units.read_text())['seen'][-1], '# the previous workspace code\n')
        self.assertEqual(json.loads((self.backups()[-1] / 'journal.json').read_text())['status'], 'rolled-back')

    def test_masked_units_are_not_enabled_or_started(self):
        units = self.home / '.config/systemd/user'
        units.mkdir(parents=True)
        (units / 'workspace-save.timer').symlink_to(os.devnull)
        out = self.run_install()
        self.assertIn('workspace-save.timer is masked', out)
        log = json.loads(self.units.read_text())['log']
        self.assertFalse([line for line in log if 'workspace-save.timer' in line and not line.startswith('is-')], log)
        self.assertEqual(self.systemd('workspace-save.timer')['enabled'], 'masked')
        self.assertEqual(self.unit('workspace-monitor.service'), {'enabled': 'enabled', 'active': 'active'})

    def test_agent_folders_set_only_in_the_terminal_are_pointed_out(self):
        with mock.patch.dict(os.environ, {'CODEX_HOME': str(self.home / 'codex-elsewhere')}):
            out = self.run_install()
        self.assertIn('CODEX_HOME is set in this terminal but not for the whole desktop session', out)

    def install_with_monitor_restart(self):
        old = self.home / '.local/share/workspace/workspace.py'
        old.parent.mkdir(parents=True)
        old.write_text('# the previous workspace code\n')
        self.units.write_text(json.dumps({'units': {'workspace-monitor.service': {'enabled': 'enabled', 'active': 'active'}}}))
        self.run_install()
        return old

    def set_unit_state(self, unit, **values):
        state = json.loads(self.units.read_text())
        state['units'][unit].update(values)
        self.units.write_text(json.dumps(state))

    def test_a_first_rollback_leaves_a_service_you_stopped_stopped(self):
        old = self.install_with_monitor_restart()
        self.set_unit_state('workspace-monitor.service', active='inactive')     # you stopped it after installing
        logged = len(json.loads(self.units.read_text())['log'])
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rollback(Options(home=self.home, real=False), 'last', desktop=self.desktop)
        self.assertIn('workspace-monitor.service (stopped since installing; left stopped)', out.getvalue())
        self.assertEqual(self.unit('workspace-monitor.service'), {'enabled': 'enabled', 'active': 'inactive'})
        later = json.loads(self.units.read_text())['log'][logged:]
        self.assertFalse([line for line in later if line.split()[-1] == 'workspace-monitor.service'
                          and line.split()[0] in ('start', 'restart', 'try-restart')], later)
        self.assertEqual(old.read_text(), '# the previous workspace code\n')

    def test_a_retry_starts_only_the_services_the_rollback_meant_to(self):
        self.install_with_monitor_restart()
        self.set_unit_state('workspace-monitor.service', active='inactive')     # you stopped it after installing
        state = json.loads(self.units.read_text())
        state['fail'] = ['disable ai-memory-snapshot.timer']
        self.units.write_text(json.dumps(state))
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(transaction.RollbackIncomplete) as stopped:
            rollback(Options(home=self.home, real=False), 'last', desktop=self.desktop)
        self.assertEqual(stopped.exception.restored, ['stopped ai-memory-snapshot.timer'])
        journal = json.loads((self.backups()[-1] / 'journal.json').read_text())
        self.assertEqual(journal['rollbacks'][-1]['restored'], ['stopped ai-memory-snapshot.timer'])
        self.assertEqual(journal['rollbacks'][-1]['services'], [])
        state = json.loads(self.units.read_text())
        state['fail'] = []
        self.units.write_text(json.dumps(state))
        with contextlib.redirect_stdout(io.StringIO()):
            rollback(Options(home=self.home, real=False), 'last', desktop=self.desktop)
        self.assertEqual(self.unit('workspace-monitor.service'), {'enabled': 'enabled', 'active': 'inactive'})
        self.assertEqual(self.unit('ai-memory-snapshot.timer'), {'enabled': 'disabled', 'active': 'inactive'})

    def test_a_runtime_mask_prevents_the_unit_file_and_its_activation(self):
        self.units.write_text(json.dumps({'units': {'workspace-save.timer': {'enabled': 'masked-runtime',
                                                                             'active': 'inactive'}}}))
        out = self.run_install()
        self.assertIn('workspace-save.timer is masked (systemd reports it as masked-runtime)', out)
        self.assertFalse((self.home / '.config/systemd/user/workspace-save.timer').exists())
        self.assertTrue((self.home / '.config/systemd/user/workspace-save.service').exists())
        log = json.loads(self.units.read_text())['log']
        self.assertFalse([line for line in log if 'workspace-save.timer' in line and not line.startswith('is-')], log)
        self.assertEqual(self.unit('workspace-save.timer'), {'enabled': 'masked-runtime', 'active': 'inactive'})
        self.assertIn('nothing was changed', self.run_install())

    def test_a_service_changed_later_is_not_overwritten(self):
        self.run_install()
        state = json.loads(self.units.read_text())
        state['units']['workspace-save.timer'] = {'enabled': 'masked', 'active': 'inactive'}
        self.units.write_text(json.dumps(state))
        with self.assertRaises(transaction.RollbackRefused) as refused:
            rollback(Options(home=self.home, real=False), 'last', desktop=self.desktop)
        self.assertIn('service workspace-save.timer changed after installation', str(refused.exception))
        self.assertTrue((self.home / '.local/bin/workspace-control').exists())
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rollback(Options(home=self.home, real=False), 'last', desktop=self.desktop, keep_changed=True)
        self.assertIn('workspace-save.timer changed after installation (now masked) (kept)', out.getvalue())
        self.assertEqual(self.unit('workspace-save.timer'), {'enabled': 'masked', 'active': 'inactive'})
        self.assertEqual(self.unit('ai-memory-snapshot.timer'), {'enabled': 'disabled', 'active': 'inactive'})
        self.assertFalse((self.home / '.local/bin/workspace-control').exists())


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
    # Stand-ins with the interface lines the reviewed official scripts have (see packages.CONTRACTS).
    CLAUDE = '''#!/bin/bash
TARGET="$1"  # Optional target parameter
if [[ -n "$TARGET" ]] && [[ ! "$TARGET" =~ ^(stable|latest|[0-9]+\\.[0-9]+\\.[0-9]+(-[^[:space:]]+)?)$ ]]; then exit 1; fi
[ "$TARGET" = "stable" ] || { echo "unexpected release $TARGET" >&2; exit 3; }
mkdir -p "$HOME/.local/bin"
printf '#!/bin/sh\\necho "9.9.9 (Claude Code)"\\n' > "$HOME/.local/bin/claude"
chmod +x "$HOME/.local/bin/claude"
: '"$binary_path" install ${TARGET:+"$TARGET"}'
'''
    CODEX = '''#!/bin/sh
NON_INTERACTIVE="${CODEX_NON_INTERACTIVE:-false}"
BIN_DIR="${CODEX_INSTALL_DIR:-$HOME/.local/bin}"
case "$2" in "" | latest) ;; esac
case ":$PATH:" in *":$BIN_DIR:"*) ;; esac
printf '%s\\n' "$2" | grep -Eq '^[0-9]+\\.[0-9]+\\.[0-9]+(-alpha(\\.[0-9]+){0,2}|-beta(\\.[0-9]+)?)?$' || exit 1
case "$1" in --release) ;; *) exit 3 ;; esac
[ "$2" = "1.2.3" ] && [ "$NON_INTERACTIVE" = "1" ] || exit 3
mkdir -p "$BIN_DIR"
printf '#!/bin/sh\\necho "codex-cli 1.2.3"\\n' > "$BIN_DIR/codex"
chmod +x "$BIN_DIR/codex"
'''

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='workbench-agents-'))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        site = self.site = self.root / 'site'
        for name, body in (('claude', self.CLAUDE), ('codex', self.CODEX)):
            (site / name).mkdir(parents=True)
            (site / name / 'install.sh').write_text(body)
        class Vendor(http.server.SimpleHTTPRequestHandler):
            def do_GET(self):
                if 'Python-urllib' in self.headers.get('User-Agent', ''):   # as the real download hosts do
                    self.send_error(403)
                    return
                super().do_GET()

            def log_message(self, *args):
                pass

        handler = lambda *a, **k: Vendor(*a, directory=str(site), **k)  # noqa: E731
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

    def test_a_changed_installer_is_not_run(self):
        changed = self.CLAUDE.replace('TARGET="$1"', 'TARGET="${1:-}"').replace('mkdir -p', 'touch "$HOME/ran"; mkdir -p')
        (self.site / 'claude/install.sh').write_text(changed)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError) as refused:
            packages.install_agent(self.agent('claude', 'stable'), self.home, self.downloads)
        self.assertIn('changed since it was reviewed', str(refused.exception))
        self.assertFalse((self.home / 'ran').exists())

    def test_a_release_the_installer_rejects_is_not_run(self):
        agent = self.agent('codex', '1.2.3')
        text = self.CODEX.replace('-beta', '-rc')
        self.assertEqual(packages.verify_installer(agent, text), [])
        agent.version = '1.2.3-beta.1'
        self.assertIn('does not accept the release "1.2.3-beta.1"', packages.verify_installer(agent, text)[0])

    @unittest.skipUnless(os.environ.get('WORKBENCH_ONLINE') == '1', 'set WORKBENCH_ONLINE=1 to fetch the real installers')
    def test_the_official_installers_still_fit(self):
        """Downloads (never runs) the vendors' current scripts and checks the reviewed interface."""
        component = load().components['agents']
        samples = {'claude': ['latest', 'stable', '2.1.89'], 'codex': ['latest', '0.162.1', '0.163.0-beta.1']}
        for name, releases in samples.items():
            for release in releases:
                agent = next(a for a in packages.agents_from(component, {name: release}) if a.name == name)
                script, _ = packages.download(agent.installer, self.downloads)
                self.assertEqual(packages.verify_installer(agent, script.read_text()), [], (name, release))
                script.unlink()

    def test_a_failing_installer_is_reported(self):
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
            packages.install_agent(self.agent('claude', 'latest'), self.home, self.downloads)

    def test_an_installer_that_leaves_a_broken_cli_is_not_successful(self):
        script = self.CLAUDE.replace('echo "9.9.9 (Claude Code)"', 'exit 139')
        (self.site / 'claude/install.sh').write_text(script)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, 'exit 139'):
            packages.install_agent(self.agent('claude', 'stable'), self.home, self.downloads)

    def test_existing_unusable_clis_require_repair_without_being_overwritten(self):
        binary = self.home / '.local/bin/claude'
        binary.parent.mkdir(parents=True)
        for body, message in (('#!/bin/sh\nexit 139\n', 'exit 139'),
                              ('#!/bin/sh\nexit 0\n', 'no version output'),
                              ('not an executable format\n', 'could not finish')):
            with self.subTest(message=message):
                binary.write_text(body)
                binary.chmod(0o755)
                run = Run(Options(home=self.home, real=False, agents=True))
                with mock.patch.object(packages, 'download', side_effect=AssertionError('downloaded')), \
                        self.assertRaisesRegex(RuntimeError, message):
                    run.install_agents()
                self.assertEqual(binary.read_text(), body)

    def test_version_timeout_reports_repair_instructions(self):
        with mock.patch.object(packages.subprocess, 'run',
                               side_effect=subprocess.TimeoutExpired(['claude', '--version'], 60)), \
                self.assertRaisesRegex(RuntimeError, 'Repair that CLI'):
            packages.version_of('claude')

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


class ClaudeHooks(unittest.TestCase):
    HOOK = {'type': 'command', 'command': '/home/x/.local/bin/workspace-control hook', 'timeout': 3}
    WANTED = json.dumps({'hooks': {'PreToolUse': [{'hooks': [HOOK]}]}})

    def merged(self, groups):
        result = json.loads(merges.claude_settings(json.dumps({'hooks': {'PreToolUse': groups}}), self.WANTED))
        return result['hooks']['PreToolUse']

    def test_a_restricted_group_does_not_count_as_the_general_hook(self):
        restricted = {'matcher': 'Bash', 'hooks': [dict(self.HOOK)]}
        self.assertEqual(self.merged([restricted]), [restricted, {'hooks': [self.HOOK]}])

    def test_an_equivalent_general_group_is_kept_as_it_is(self):
        for matcher in ({}, {'matcher': ''}, {'matcher': '*'}):
            general = {**matcher, 'hooks': [{'type': 'command', 'command': self.HOOK['command'], 'timeout': 10}]}
            self.assertEqual(self.merged([general]), [general])

    def test_a_different_hook_type_is_not_the_same_hook(self):
        prompt = {'hooks': [{'type': 'prompt', 'prompt': 'check', 'command': self.HOOK['command']}]}
        self.assertEqual(len(self.merged([prompt])), 2)

    def test_merging_twice_changes_nothing(self):
        once = merges.claude_settings('{}', self.WANTED)
        self.assertEqual(merges.claude_settings(once, self.WANTED), once)


if __name__ == '__main__':
    unittest.main(verbosity=2)
