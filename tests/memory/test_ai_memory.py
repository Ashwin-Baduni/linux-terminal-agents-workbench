#!/usr/bin/env python3
"""Tests for the staged ai-memory helper, each run against a fresh temporary home folder."""
import contextlib
import importlib.machinery
import io
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import support  # noqa: E402

SCRIPT = support.home() / '.local/bin/ai-memory'
GLOBAL_INDEX = '''# Global memory

Rules here.

## Index
- [User profile](user-profile.md) — who
- [Machine](machine.md) — what
'''


class Base(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix='aimem-'))
        self.addCleanup(shutil.rmtree, self.home)
        self.memory = self.home / 'ai-memory'
        self.memory.mkdir()
        (self.memory / 'MEMORY.md').write_text(GLOBAL_INDEX)
        (self.memory / 'user-profile.md').write_text('profile\n')
        self.env = {**os.environ, 'HOME': str(self.home), 'GIT_CONFIG_NOSYSTEM': '1'}
        self.env.pop('XDG_CONFIG_HOME', None)
        self.env.pop('GIT_CONFIG_GLOBAL', None)

    def run_tool(self, *args, cwd=None, ok=True):
        result = subprocess.run([str(SCRIPT), *args], cwd=cwd or self.home, env=self.env,
                                capture_output=True, text=True)
        if ok is not None:
            self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
        return result

    def folder(self, *parts):
        path = self.home.joinpath(*parts)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def index(self):
        return (self.memory / 'MEMORY.md').read_text()

    def project_lines(self):
        return [line for line in self.index().splitlines() if line.startswith('- [') and '(projects/' in line]

    def git(self, folder, *args):
        return subprocess.run(['git', '-C', str(folder), *args], env=self.env, capture_output=True, text=True)

    def claude_memory(self, folder):
        return self.home / '.claude/projects' / re.sub(r'[^A-Za-z0-9]', '-', str(folder)) / 'memory'


class Link(Base):
    def test_plain_folder(self):
        folder = self.folder('Desktop', 'ClientResearch')
        out = self.run_tool('link', cwd=folder).stdout
        self.assertIn('Project memory: ~/ai-memory/projects/client-research', out)
        self.assertEqual((folder / '.ai-memory').read_text(), '~/ai-memory/projects/client-research\n')
        self.assertEqual(json.loads((folder / '.claude/settings.local.json').read_text()),
                         {'autoMemoryDirectory': '~/ai-memory/projects/client-research'})
        self.assertEqual((self.memory / 'projects/client-research/MEMORY.md').read_text(), '')
        ignore = (self.home / '.config/git/ignore').read_text().splitlines()
        self.assertIn('.ai-memory', ignore)
        self.assertIn('**/.claude/settings.local.json', ignore)
        # The global index keeps its rules and Index, and gains a Projects section at the end.
        text = self.index()
        self.assertTrue(text.startswith(GLOBAL_INDEX.rstrip('\n')))
        self.assertTrue(text.endswith('## Projects\nPrivate memory for one folder each, in `projects/<name>/`. '
                                      '`ai-memory status` checks the links.\n'
                                      '- [client-research](projects/client-research/MEMORY.md) — `~/Desktop/ClientResearch`\n'))

    def test_second_run_changes_nothing(self):
        folder = self.folder('proj')
        self.run_tool('link', '--about', 'a test project', cwd=folder)
        before = {p: p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        out = self.run_tool('link', cwd=folder).stdout
        after = {p: p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertNotIn('listed under', out)
        self.assertNotIn('autoMemoryDirectory', out)

    def test_moves_claude_notes_in(self):
        folder = self.folder('Desktop', 'website-refresh')
        old = self.claude_memory(folder)
        old.mkdir(parents=True)
        (old / 'MEMORY.md').write_text('- [A](a.md) — first\n- [B](b.md) — second\n')
        (old / 'a.md').write_text('note a\n')
        (old / 'b.md').write_text('note b\n')
        out = self.run_tool('link', cwd=folder).stdout
        self.assertIn('moved in 2 notes', out)
        target = self.memory / 'projects/website-refresh'
        self.assertEqual((target / 'a.md').read_text(), 'note a\n')
        self.assertEqual((target / 'MEMORY.md').read_text(), '- [A](a.md) — first\n- [B](b.md) — second\n')
        self.assertFalse(old.exists())
        self.assertTrue(old.parent.exists())  # Claude's session history next to it is untouched.

    def test_conflicting_note_stops_before_moving(self):
        folder = self.folder('p')
        old = self.claude_memory(folder)
        old.mkdir(parents=True)
        (old / 'a.md').write_text('claude version\n')
        (old / 'b.md').write_text('b\n')
        target = self.memory / 'projects/p'
        target.mkdir(parents=True)
        (target / 'a.md').write_text('different\n')
        err = self.run_tool('link', cwd=folder, ok=False).stderr
        self.assertIn('differs', err)
        self.assertTrue((old / 'a.md').exists() and (old / 'b.md').exists())
        self.assertFalse((target / 'b.md').exists())
        self.assertFalse((folder / '.ai-memory').exists())

    def test_identical_note_is_merged(self):
        folder = self.folder('p')
        old = self.claude_memory(folder)
        old.mkdir(parents=True)
        (old / 'a.md').write_text('same\n')
        (old / 'MEMORY.md').write_text('- [A](a.md) — a\n')
        target = self.memory / 'projects/p'
        target.mkdir(parents=True)
        (target / 'a.md').write_text('same\n')
        (target / 'MEMORY.md').write_text('- [A](a.md) — a\n- [C](c.md) — c\n')
        self.run_tool('link', cwd=folder)
        self.assertEqual((target / 'MEMORY.md').read_text(), '- [A](a.md) — a\n- [C](c.md) — c\n')
        self.assertFalse(old.exists())

    def test_existing_settings_are_kept(self):
        folder = self.folder('p')
        (folder / '.claude').mkdir()
        (folder / '.claude/settings.local.json').write_text('{"permissions": {"allow": ["Bash(ls)"]}}')
        out = self.run_tool('link', cwd=folder).stdout
        self.assertIn('autoMemoryDirectory', out)
        self.assertEqual(json.loads((folder / '.claude/settings.local.json').read_text()),
                         {'permissions': {'allow': ['Bash(ls)']}, 'autoMemoryDirectory': '~/ai-memory/projects/p'})

    def test_broken_settings_stop_everything(self):
        folder = self.folder('p')
        old = self.claude_memory(folder)
        old.mkdir(parents=True)
        (old / 'a.md').write_text('a\n')
        (folder / '.claude').mkdir()
        (folder / '.claude/settings.local.json').write_text('{oops')
        err = self.run_tool('link', cwd=folder, ok=False).stderr
        self.assertIn('not valid JSON', err)
        self.assertTrue((old / 'a.md').exists())
        self.assertFalse((folder / '.ai-memory').exists())
        self.assertFalse((self.memory / 'projects/p').exists())
        self.assertEqual(self.project_lines(), [])

    def test_names(self):
        folder = self.folder('My Cool_Project.v2')
        self.assertIn('projects/my-cool-project-v2', self.run_tool('link', cwd=folder).stdout)
        folder = self.folder('Example-Tools-Repo')
        self.assertIn('projects/workbench', self.run_tool('link', 'workbench', cwd=folder).stdout)
        self.assertIn('not a usable name', self.run_tool('link', 'Bad Name', '--dir', str(self.folder('x')), ok=False).stderr)

    def test_refuses_home_and_memory_folder(self):
        self.assertIn('global', self.run_tool('link', cwd=self.home, ok=False).stderr)
        self.assertIn('global', self.run_tool('link', '--dir', str(self.memory), ok=False).stderr)
        self.assertIn('global', self.run_tool('link', '--dir', '/', ok=False).stderr)

    def test_rename_keeps_memory(self):
        folder = self.folder('Desktop', 'old-name')
        self.run_tool('link', 'proj', '--about', 'keeps this', cwd=folder)
        (self.memory / 'projects/proj/note.md').write_text('a fact\n')
        moved = self.home / 'Desktop' / 'new name'
        folder.rename(moved)
        # The marker moved with the folder, so both agents still find the memory...
        self.assertEqual(self.run_tool('where', cwd=moved).stdout.strip(), str(self.memory / 'projects/proj'))
        # ...and status points out the stale path in the index.
        result = self.run_tool('status', ok=False)
        self.assertIn('folder not found', result.stdout)
        out = self.run_tool('link', cwd=moved).stdout
        self.assertIn('listed under', out)
        self.assertEqual(self.project_lines(), ['- [proj](projects/proj/MEMORY.md) — `~/Desktop/new name` · keeps this'])
        self.assertIn('ok', self.run_tool('status').stdout)
        self.assertEqual((self.memory / 'projects/proj/note.md').read_text(), 'a fact\n')

    def test_relink_after_marker_lost(self):
        """After a reinstall the memory is restored but the folder's marker is gone."""
        folder = self.folder('p')
        self.run_tool('link', cwd=folder)
        (folder / '.ai-memory').unlink()
        shutil.rmtree(folder / '.claude')
        self.assertIn('no .ai-memory marker', self.run_tool('status', ok=False).stdout)
        self.run_tool('link', 'p', cwd=folder)
        self.assertIn('ok', self.run_tool('status').stdout)

    def test_name_already_taken(self):
        self.run_tool('link', 'shared', cwd=self.folder('a'))
        err = self.run_tool('link', 'shared', cwd=self.folder('b'), ok=False).stderr
        self.assertIn('already belongs to ~/a', err)
        self.assertFalse((self.home / 'b/.ai-memory').exists())

    def test_folder_already_linked_to_another_name(self):
        folder = self.folder('a')
        self.run_tool('link', 'one', cwd=folder)
        self.assertIn('already uses the project memory "one"', self.run_tool('link', 'two', cwd=folder, ok=False).stderr)

    def test_nested(self):
        parent = self.folder('umbrella')
        child = self.folder('umbrella', 'repo', 'src')
        self.run_tool('link', cwd=parent)
        self.assertEqual(self.run_tool('where', cwd=child).stdout.strip(), str(self.memory / 'projects/umbrella'))
        err = self.run_tool('link', cwd=child, ok=False).stderr
        self.assertIn('already gives this folder its memory ("umbrella")', err)
        self.run_tool('link', '--nested', cwd=child)
        self.assertEqual(self.run_tool('where', cwd=child).stdout.strip(), str(self.memory / 'projects/src'))

    def test_where_outside_any_project(self):
        result = self.run_tool('where', cwd=self.folder('nothing'), ok=False)
        self.assertEqual((result.returncode, result.stdout), (1, ''))

    def test_projects_sorted_and_index_untouched(self):
        for name in ('beta', 'alpha', 'gamma'):
            self.run_tool('link', cwd=self.folder(name))
        self.assertEqual([l.split(']')[0][3:] for l in self.project_lines()], ['alpha', 'beta', 'gamma'])
        self.assertTrue(self.index().startswith(GLOBAL_INDEX))

    def test_projects_section_followed_by_another_section(self):
        (self.memory / 'MEMORY.md').write_text(GLOBAL_INDEX + '\n## Projects\nintro\n- [m](projects/m/MEMORY.md) — `~/m`\n\n## Later\ntext\n')
        self.run_tool('link', cwd=self.folder('z'))
        self.run_tool('link', cwd=self.folder('a'))
        self.assertTrue(self.index().endswith('## Projects\nintro\n- [a](projects/a/MEMORY.md) — `~/a`\n'
                                              '- [m](projects/m/MEMORY.md) — `~/m`\n- [z](projects/z/MEMORY.md) — `~/z`\n'
                                              '\n## Later\ntext\n'))


class Git(Base):
    def repo(self, *parts):
        folder = self.folder(*parts)
        self.git(folder, 'init', '-q')
        return folder

    def untracked(self, folder):
        return self.git(folder, 'status', '--porcelain', '--untracked-files=all').stdout

    def test_repo_ignores_marker_and_settings(self):
        folder = self.repo('repo')
        (folder / 'README.md').write_text('hi\n')
        out = self.run_tool('link', cwd=folder).stdout
        self.assertNotIn('warning', out)
        self.assertEqual(self.untracked(folder), '?? README.md\n')

    def test_project_inside_a_repo(self):
        top = self.repo('mono')
        folder = self.folder('mono', 'apps', 'web')
        self.run_tool('link', cwd=folder)
        self.assertEqual(self.untracked(top), '')

    def test_respects_configured_excludes_file(self):
        config = self.home / '.gitconfig'
        config.write_text('[core]\n\texcludesfile = ~/my-ignore\n')
        folder = self.repo('repo')
        self.run_tool('link', cwd=folder)
        self.assertIn('.ai-memory', (self.home / 'my-ignore').read_text())
        self.assertFalse((self.home / '.config/git/ignore').exists())
        self.assertEqual(self.untracked(folder), '')

    def test_repository_level_excludes_setting(self):
        folder = self.repo('repo')
        self.git(folder, 'config', 'core.excludesFile', str(self.home / 'elsewhere'))
        out = self.run_tool('link', cwd=folder).stdout
        self.assertNotIn('warning', out)
        self.assertIn('/.ai-memory', (folder / '.git/info/exclude').read_text())
        self.assertEqual(self.untracked(folder), '')

    def test_gitignore_unignores(self):
        folder = self.repo('repo')
        (folder / '.gitignore').write_text('!.ai-memory\n')
        out = self.run_tool('link', cwd=folder).stdout
        self.assertIn("warning: .ai-memory is not ignored here", out)

    def test_already_committed(self):
        folder = self.repo('repo')
        (folder / '.ai-memory').write_text('~/ai-memory/projects/repo\n')
        self.git(folder, 'add', '-f', '.ai-memory')
        out = self.run_tool('link', cwd=folder).stdout
        self.assertIn('already committed', out)


class Snapshot(Base):
    def log(self):
        return self.git(self.memory, 'log', '--format=%an|%s%n%b').stdout

    def test_first_snapshot_creates_history(self):
        self.run_tool('snapshot')
        self.assertTrue((self.memory / '.git').is_dir())
        log = self.log()
        self.assertIn('ai-memory|Snapshot ', log)
        self.assertIn('A\tMEMORY.md', log)
        self.assertIn('A\tuser-profile.md', log)

    def test_nothing_changed_no_commit(self):
        self.run_tool('snapshot')
        self.run_tool('snapshot')
        self.assertEqual(self.git(self.memory, 'rev-list', '--count', 'HEAD').stdout.strip(), '1')

    def test_change_and_delete_are_recorded_and_restorable(self):
        self.run_tool('snapshot')
        (self.memory / 'user-profile.md').unlink()
        (self.memory / 'MEMORY.md').write_text('changed\n')
        project = self.memory / 'projects/p'
        project.mkdir(parents=True)
        (project / 'n.md').write_text('n\n')
        self.run_tool('snapshot')
        last = self.git(self.memory, 'log', '-1', '--format=%b').stdout
        self.assertIn('D\tuser-profile.md', last)
        self.assertIn('M\tMEMORY.md', last)
        self.assertIn('A\tprojects/p/n.md', last)
        self.git(self.memory, 'checkout', 'HEAD~1', '--', 'user-profile.md')
        self.assertEqual((self.memory / 'user-profile.md').read_text(), 'profile\n')

    def test_uses_own_identity_when_set(self):
        (self.home / '.gitconfig').write_text('[user]\n\tname = Someone\n\temail = s@example.com\n')
        self.run_tool('snapshot')
        self.assertIn('Someone|Snapshot', self.log())

    def test_busy_repository_is_skipped(self):
        self.run_tool('snapshot')
        (self.memory / 'x.md').write_text('x\n')
        (self.memory / '.git/index.lock').write_text('')
        self.run_tool('snapshot')
        (self.memory / '.git/index.lock').unlink()
        self.assertEqual(self.git(self.memory, 'rev-list', '--count', 'HEAD').stdout.strip(), '1')
        self.run_tool('snapshot')
        self.assertEqual(self.git(self.memory, 'rev-list', '--count', 'HEAD').stdout.strip(), '2')

    def test_status_lists_unlinked_claude_notes(self):
        folder = self.folder('q')
        old = self.claude_memory(folder)
        old.mkdir(parents=True)
        (old / 'MEMORY.md').write_text('')
        (old / 'a.md').write_text('a\n')
        empty = self.claude_memory(self.home)
        empty.mkdir(parents=True)
        (empty / 'MEMORY.md').write_text('')
        out = self.run_tool('status').stdout
        self.assertIn('not linked yet: ~/.claude/projects/-', out)
        self.assertIn('(1 Claude note)', out)
        self.assertEqual(out.count('not linked yet'), 1)


class Concurrency(Base):
    """Agents link folders at the same moment; the global index must keep every project."""

    def names(self):
        return sorted(line.split(']')[0][3:] for line in self.project_lines())

    def waiting_for_lock(self, pid):
        """True once the process is queued on a file lock (Linux lists waiters with ->)."""
        return any('->' in fields and str(pid) in fields
                   for fields in map(str.split, Path('/proc/locks').read_text().splitlines()))

    def test_link_waits_while_another_updates_the_index(self):
        """A second link that starts while the first has read the index but not yet written it."""
        first, second = self.folder('first'), self.folder('second')
        loader = importlib.machinery.SourceFileLoader('ai_memory_under_test', str(SCRIPT))
        other = []
        with mock.patch.dict(os.environ, self.env, clear=True):
            tool = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
            loader.exec_module(tool)  # Reads HOME now, so it uses this test's home.
            write = tool.write

            def paused_write(path, text):
                if path == tool.INDEX and not other:
                    # The first link holds its view of the index. Start the second link now and let it
                    # run until it either finishes (the old race) or queues behind the first.
                    other.append(subprocess.Popen([str(SCRIPT), 'link'], cwd=second, env=self.env,
                                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
                    deadline = time.monotonic() + 30
                    while other[0].poll() is None and not self.waiting_for_lock(other[0].pid):
                        self.assertLess(time.monotonic(), deadline, 'The second link neither finished nor waited.')
                        time.sleep(0.01)
                write(path, text)

            tool.write = paused_write
            with mock.patch.object(sys, 'argv', ['ai-memory', 'link', '--dir', str(first)]), \
                    contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as finished:
                tool.main()
        self.assertEqual(finished.exception.code, 0)
        out, err = other[0].communicate(timeout=30)
        self.assertEqual(other[0].returncode, 0, out + err)
        self.assertEqual(self.names(), ['first', 'second'])
        self.assertIn('ok', self.run_tool('status').stdout)

    def test_many_links_at_once(self):
        folders = [self.folder(f'project-{n}') for n in range(8)]
        runs = [subprocess.Popen([str(SCRIPT), 'link'], cwd=folder, env=self.env, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True) for folder in folders]
        for run in runs:
            out, err = run.communicate(timeout=60)
            self.assertEqual(run.returncode, 0, out + err)
        self.assertEqual(self.names(), sorted(folder.name for folder in folders))


if __name__ == '__main__':
    unittest.main(verbosity=2)
