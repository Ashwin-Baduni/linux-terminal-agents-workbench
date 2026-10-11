#!/usr/bin/env python3
"""The manifest, the staged layout, and the shipped files' own validators."""
import fnmatch
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import support  # noqa: E402
from installer.manifest import load, problems, TOKEN  # noqa: E402
from installer.stage import stage, values_for  # noqa: E402

HOME = support.home()
REPO = support.REPO
MANIFEST = load(REPO)
REAL_HOME = pwd.getpwuid(os.getuid()).pw_dir


def scratch(test):
    folder = Path(tempfile.mkdtemp(prefix='workbench-repo-'))
    test.addCleanup(shutil.rmtree, folder, ignore_errors=True)
    return folder


class Manifest(unittest.TestCase):
    def test_sound(self):
        self.assertEqual(problems(MANIFEST), [])

    def test_default_includes_every_component(self):
        self.assertEqual(set(MANIFEST.closure(MANIFEST.default)), set(MANIFEST.components))

    def test_dependencies(self):
        self.assertEqual(MANIFEST.closure(['agents']), ['terminal', 'workspace', 'agents'])
        self.assertEqual(MANIFEST.closure(['memory']), ['memory'])
        with self.assertRaises(ValueError):
            MANIFEST.closure(['nothing'])

    def test_executables_are_executable_in_the_repository(self):
        for entry in MANIFEST.entries:
            if entry.source and entry.mode & 0o100:
                self.assertTrue(os.access(REPO / entry.source, os.X_OK), entry.source)

    def test_clipboard_identity(self):
        component = MANIFEST.components['clipboard']
        uuid = component['extension']
        metadata = REPO / 'payload/clipboard' / uuid / 'metadata.json'
        data = json.loads(metadata.read_text())
        self.assertEqual(data['uuid'], uuid)
        self.assertEqual(data['settings-schema'], component['settings-schema'])
        self.assertTrue((metadata.parent / 'schemas' / (component['settings-schema'] + '.gschema.xml')).is_file())
        settings = json.loads((REPO / component['gsettings']).read_text())
        self.assertIn(['org.gnome.shell', 'enabled-extensions', [uuid]], settings['add'])

    def test_gsettings_files(self):
        for name, component in MANIFEST.components.items():
            if 'gsettings' not in component:
                continue
            data = json.loads((REPO / component['gsettings']).read_text())
            self.assertLessEqual(set(data), {'set', 'add', 'remove'}, name)
            for operation, rows in data.items():
                for schema, key, value in rows:
                    self.assertIsInstance(value, str if operation == 'set' else list, (name, schema, key))

    def test_agent_policy_is_opt_in(self):
        """The default agent settings carry no permission policy or model choice."""
        claude = json.loads((REPO / 'payload/agents/claude-settings.json').read_text())
        codex = json.loads((REPO / 'payload/agents/codex-config.json').read_text())
        for key in ('permissions', 'skipDangerousModePermissionPrompt', 'model', 'modelSettings', 'effortLevel'):
            self.assertNotIn(key, claude)
        for key in ('approval_policy', 'sandbox_mode', 'model', 'model_reasoning_effort', 'web_search'):
            self.assertNotIn(key, codex.get('', {}))
        self.assertNotIn('notice', codex)
        preset = json.loads((REPO / MANIFEST.components['agents']['presets']['full-access']).read_text())
        self.assertEqual(preset['claude']['permissions'], {'defaultMode': 'bypassPermissions'})
        self.assertEqual(preset['codex']['']['sandbox_mode'], 'danger-full-access')


class Staged(unittest.TestCase):
    """The home tests/run.py staged with the default components."""

    def test_every_entry_landed(self):
        for entry in MANIFEST.select():
            path = HOME / entry.target
            if entry.kind == 'merge':
                continue
            if entry.kind == 'link':
                self.assertTrue(path.is_symlink(), entry.target)
                self.assertEqual(os.readlink(path), entry.link)
                self.assertTrue(path.resolve().is_relative_to(HOME), entry.target)
                continue
            if entry.kind == 'dir':
                self.assertTrue(path.is_dir(), entry.target)
                continue
            self.assertTrue(path.is_file(), entry.target)
            self.assertEqual(path.stat().st_mode & 0o777, entry.mode, entry.target)

    def test_rendered_for_the_staged_home_only(self):
        for entry in MANIFEST.select():
            if not entry.source or entry.kind == 'merge':
                continue
            data = (HOME / entry.target).read_bytes()
            self.assertFalse(set(TOKEN.findall(data)) & {n.encode() for n in MANIFEST.placeholders}, entry.target)
            if entry.render:
                self.assertIn(str(HOME).encode(), data, entry.target)
            # Nothing staged may point back at the account running the tests.
            self.assertNotIn((REAL_HOME + '/').encode(), data, entry.target)

    def test_claude_handler_uses_the_target_home(self):
        text = (HOME / '.local/share/applications/claude-code-url-handler.desktop').read_text()
        self.assertIn(f'Exec="{HOME}/.local/bin/claude" --handle-uri %u', text)

    def test_sidebar_theme_follows_the_main_theme(self):
        link = HOME / '.config/yazi-sidebar/theme.toml'
        self.assertEqual(link.read_text(), (HOME / '.config/yazi/theme.toml').read_text())
        self.assertIn(f'syntect_theme = "{HOME}/.config/yazi/Catppuccin-Mocha.tmTheme"', link.read_text())

    def test_units_use_the_unit_home(self):
        for name in ('workspace-monitor.service', 'workspace-save.service', 'ai-memory-snapshot.service'):
            text = (HOME / '.config/systemd/user' / name).read_text()
            self.assertIn('ExecStart=%h/.local/bin/', text, name)

    def test_python_compiles(self):
        for path in [HOME / '.local/share/workspace/workspace.py',
                     *[p for p in (HOME / '.local/bin').iterdir() if p.read_bytes().startswith(b'#!/usr/bin/env python3')]]:
            compile(path.read_bytes(), str(path), 'exec')

    def test_shell_syntax(self):
        scripts = [p for p in (HOME / '.local/bin').iterdir() if p.read_bytes().startswith((b'#!/bin/bash', b'#!/bin/sh'))]
        scripts += [HOME / '.local/share/nautilus/scripts/Open Workspace Here', REPO / 'payload/terminal/shell/bashrc.sh',
                    HOME / '.local/share/workspace/bash-preexec.sh']
        for path in scripts:
            result = subprocess.run(['bash', '-n', str(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, f'{path}: {result.stderr}')

    def test_data_files_parse(self):
        for entry in MANIFEST.entries:
            if not entry.source:
                continue
            data = (REPO / entry.source).read_bytes()
            if entry.render:
                data = MANIFEST.render(entry, values_for(HOME))
            if entry.source.endswith('.json'):
                json.loads(data)
            elif entry.source.endswith('.toml'):
                tomllib.loads(data.decode())

    @unittest.skipUnless(shutil.which('desktop-file-validate'), 'desktop-file-validate is not installed')
    def test_desktop_entries(self):
        for path in (HOME / '.local/share/applications').glob('*.desktop'):
            result = subprocess.run(['desktop-file-validate', str(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, f'{path.name}: {result.stdout}{result.stderr}')

    @unittest.skipUnless(shutil.which('glib-compile-schemas'), 'glib-compile-schemas is not installed')
    def test_clipboard_schema_compiles(self):
        schemas = HOME / '.local/share/gnome-shell/extensions' / MANIFEST.components['clipboard']['extension'] / 'schemas'
        result = subprocess.run(['glib-compile-schemas', '--strict', '--dry-run', str(schemas)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which('ghostty'), 'Ghostty is not installed')
    def test_ghostty_config(self):
        result = subprocess.run(['ghostty', '+validate-config', f'--config-file={HOME}/.config/ghostty/config'],
                                capture_output=True, text=True, env=dict(os.environ, HOME=str(HOME)))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which('tmux'), 'tmux is not installed')
    def test_tmux_configs_load(self):
        for name in ('workspace.conf', 'agent-terminal.conf'):
            socket = str(scratch(self) / 'tmux.sock')
            result = subprocess.run(['tmux', '-S', socket, '-f', str(HOME / '.config/tmux' / name),
                                     'new-session', '-d', 'sleep 30'], capture_output=True, text=True)
            subprocess.run(['tmux', '-S', socket, 'kill-server'], capture_output=True)
            self.assertEqual(result.returncode, 0, f'{name}: {result.stderr}')


class Staging(unittest.TestCase):
    def test_seed_never_overwrites(self):
        home = scratch(self) / 'home'
        (home / 'ai-memory').mkdir(parents=True)
        (home / 'ai-memory/MEMORY.md').write_text('my notes\n')
        stage(home, ['memory'])
        self.assertEqual((home / 'ai-memory/MEMORY.md').read_text(), 'my notes\n')
        self.assertTrue((home / 'ai-memory/projects').is_dir())

    def test_only_the_chosen_components(self):
        home = scratch(self) / 'home'
        written = stage(home, ['memory'])
        self.assertTrue(written)
        self.assertTrue(all(MANIFEST_TARGETS[target] == 'memory' for target in written))
        self.assertFalse((home / '.config/ghostty').exists())

    def test_refuses_the_real_home_and_unsafe_paths(self):
        with self.assertRaises(ValueError):
            stage(REAL_HOME, ['memory'])
        with self.assertRaises(ValueError):
            stage(scratch(self) / 'a home', ['memory'])

    def test_restaging_is_stable(self):
        home = scratch(self) / 'home'
        stage(home)
        before = {p: (p.read_bytes() if p.is_file() and not p.is_symlink() else os.readlink(p) if p.is_symlink() else None)
                  for p in home.rglob('*')}
        stage(home)
        after = {p: (p.read_bytes() if p.is_file() and not p.is_symlink() else os.readlink(p) if p.is_symlink() else None)
                 for p in home.rglob('*')}
        self.assertEqual(before, after)


class MemoryTemplates(unittest.TestCase):
    """A fresh install's empty memory works with the staged ai-memory helper."""

    def test_link_into_the_template_index(self):
        home = scratch(self) / 'home'
        stage(home, ['memory'])
        template = (REPO / 'templates/memory/MEMORY.md').read_text()
        self.assertEqual((home / 'ai-memory/MEMORY.md').read_text(), template)
        self.assertEqual(sorted(p.name for p in (home / 'ai-memory').iterdir()), ['MEMORY.md', 'projects'])
        folder = home / 'Project'
        folder.mkdir()
        env = dict(os.environ, HOME=str(home), GIT_CONFIG_NOSYSTEM='1')
        env.pop('XDG_CONFIG_HOME', None)
        tool = str(home / '.local/bin/ai-memory')
        result = subprocess.run([tool, 'link'], cwd=folder, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        index = (home / 'ai-memory/MEMORY.md').read_text()
        self.assertTrue(index.startswith(template.rstrip('\n')))
        self.assertTrue(index.endswith('`ai-memory status` checks the links.\n'
                                       '- [project](projects/project/MEMORY.md) — `~/Project`\n'))
        status = subprocess.run([tool, 'status'], env=env, capture_output=True, text=True)
        self.assertEqual(status.returncode, 0, status.stdout)

    def test_templates_hold_no_notes(self):
        index = (REPO / 'templates/memory/MEMORY.md').read_text()
        self.assertNotIn('- [', index.split('## Index', 1)[1])
        self.assertIn('@~/ai-memory/MEMORY.md', (REPO / 'templates/memory/CLAUDE.md').read_text())

    def test_codex_reads_the_same_rules(self):
        home = scratch(self) / 'home'
        stage(home, ['memory'])
        self.assertEqual((home / '.codex/AGENTS.md').resolve(), (home / 'ai-memory/MEMORY.md').resolve())


class ThirdParty(unittest.TestCase):
    def test_inventory_matches_the_tree(self):
        inventory = tomllib.loads((REPO / 'manifests/third-party.toml').read_text())['material']
        tracked = [str(p.relative_to(REPO)) for p in REPO.rglob('*') if p.is_file() and '.git' not in p.parts]
        for item in inventory:
            self.assertTrue((REPO / item['licence']).is_file(), item['name'])
            for pattern in item['files']:
                self.assertTrue(any(fnmatch.fnmatchcase(name, pattern) for name in tracked), (item['name'], pattern))
        covered = [pattern for item in inventory for pattern in item['files']]
        for name in tracked:
            if name.startswith('vendor/'):
                self.assertTrue(any(fnmatch.fnmatchcase(name, pattern) for pattern in covered), name)
        for licence in (REPO / 'licenses').iterdir():
            self.assertIn(str(licence.relative_to(REPO)), [item['licence'] for item in inventory])


class Publication(unittest.TestCase):
    def test_public_check_passes(self):
        result = subprocess.run([sys.executable, str(REPO / 'scripts/check-public.py')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


MANIFEST_TARGETS = {entry.target: entry.component for entry in MANIFEST.entries}

if __name__ == '__main__':
    unittest.main(verbosity=2)
