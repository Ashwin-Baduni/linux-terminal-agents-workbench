#!/usr/bin/env python3
"""The publication scan (files, history and commit metadata) and import-live's refusal to write private data."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import support  # noqa: E402

support.home()
REPO = support.REPO
CHECK = str(REPO / 'scripts/check-public.py')
IMPORT = str(REPO / 'scripts/import-live.py')


class Scratch(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='workbench-publication-'))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.patterns = self.root / 'private-patterns.txt'
        self.patterns.write_text('# private names for this test\n(?i)acme-secret-project\n')
        self.env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=str(self.root / 'gitconfig'))

    def git_repo(self):
        repo = self.root / 'repo'
        repo.mkdir()
        subprocess.run(['git', 'init', '-q', '-b', 'main', str(repo)], check=True, env=self.env)
        (repo / 'README.md').write_text('hello\n')
        return repo

    def commit(self, repo, name, email, message='Add a file'):
        subprocess.run(['git', '-C', str(repo), 'add', '-A'], check=True, env=self.env)
        identity = {'GIT_AUTHOR_NAME': name, 'GIT_AUTHOR_EMAIL': email,
                    'GIT_COMMITTER_NAME': name, 'GIT_COMMITTER_EMAIL': email}
        subprocess.run(['git', '-C', str(repo), '-c', 'commit.gpgsign=false', 'commit', '-q', '-m', message],
                       check=True, env={**self.env, **identity})

    def check(self, repo, *extra):
        return subprocess.run([sys.executable, CHECK, '--repo', str(repo), '--history',
                               '--patterns', str(self.patterns), *extra],
                              capture_output=True, text=True, env=self.env)


class History(Scratch):
    def test_personal_commit_email_is_found(self):
        repo = self.git_repo()
        self.commit(repo, 'Someone', 'someone.private@gmail.com')
        result = self.check(repo)
        self.assertEqual(result.returncode, 1)
        self.assertIn('author: email address someone.private@gmail.com', result.stdout)
        self.assertIn('committer: email address someone.private@gmail.com', result.stdout)

    def test_noreply_identity_is_clean(self):
        repo = self.git_repo()
        self.commit(repo, 'some-user', '12345+some-user@users.noreply.github.com',
                    'Add a file\n\nCo-Authored-By: Claude <noreply@anthropic.com>')
        result = self.check(repo)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('1 commits', result.stdout)

    def test_private_name_in_a_commit_message_or_name(self):
        repo = self.git_repo()
        self.commit(repo, 'some-user', '12345+some-user@users.noreply.github.com', 'Notes for acme-secret-project')
        (repo / 'other.txt').write_text('x\n')
        self.commit(repo, 'ACME-SECRET-PROJECT bot', '12345+some-user@users.noreply.github.com')
        result = self.check(repo)
        self.assertIn('message:1: private pattern', result.stdout)
        self.assertIn('author: name "ACME-SECRET-PROJECT bot" matches private pattern', result.stdout)

    def test_a_removed_file_still_counts_in_history(self):
        repo = self.git_repo()
        (repo / 'notes.txt').write_text('about acme-secret-project\n')
        self.commit(repo, 'some-user', '1+some-user@users.noreply.github.com')
        (repo / 'notes.txt').unlink()
        self.commit(repo, 'some-user', '1+some-user@users.noreply.github.com', 'Remove notes')
        self.assertEqual(subprocess.run([sys.executable, CHECK, '--repo', str(repo), '--patterns', str(self.patterns)],
                                        capture_output=True, env=self.env).returncode, 0)
        self.assertIn('notes.txt (history', self.check(repo).stdout)

    def test_secret_values_are_redacted_in_the_report(self):
        repo = self.git_repo()
        token = 'sk-ant-' + 'Zq9Xw2Lm7Rt4' * 4
        (repo / 'config.py').write_text(f'KEY = "{token}"\n')
        self.commit(repo, 'some-user', '1+some-user@users.noreply.github.com')
        result = self.check(repo)
        self.assertIn('Anthropic key: sk-a… (', result.stdout)
        self.assertNotIn(token, result.stdout + result.stderr)
        self.assertNotIn(token[:20], result.stdout + result.stderr)

    def test_a_removed_credential_file_is_found_in_history(self):
        repo = self.git_repo()
        (repo / '.env').write_text('MODE=dev\n')
        self.commit(repo, 'some-user', '1+some-user@users.noreply.github.com')
        (repo / '.env').unlink()
        self.commit(repo, 'some-user', '1+some-user@users.noreply.github.com', 'Remove it')
        result = self.check(repo)
        self.assertIn('.env (history', result.stdout)
        self.assertIn('looks like personal state, a credential or a build leftover', result.stdout)

    def test_bad_pattern_file_is_explained(self):
        self.patterns.write_text('*broken\n')
        result = self.check(self.git_repo())
        self.assertIn('not a regular expression', result.stderr)


class Allowlist(unittest.TestCase):
    def test_context_limits_an_exception_to_its_line(self):
        sys.path.insert(0, str(REPO / 'scripts'))
        import public_scan
        allow = [{'path': '*.md', 'value': 'acme', 'context': 'github.com/acme/tools'}]
        found = {'private': '(?i)acme'}
        self.assertEqual(public_scan.scan_text('a.md', 'a.md', 'git clone https://github.com/acme/tools\n', found, allow), [])
        self.assertEqual(len(public_scan.scan_text('a.md', 'a.md', 'Written for acme.\n', found, allow)), 1)
        self.assertEqual(len(public_scan.scan_text('a.py', 'a.py', 'github.com/acme/tools\n', found, allow)), 1)


class ImportLive(Scratch):
    def setUp(self):
        super().setUp()
        self.copy = self.root / 'copy'
        for folder in ('manifests', 'payload', 'vendor', 'licenses', 'templates'):
            shutil.copytree(REPO / folder, self.copy / folder)
        self.live = self.root / 'live'
        (self.live / '.config/ghostty').mkdir(parents=True)
        self.config = 'payload/terminal/ghostty/config'

    def run_import(self, *args):
        return subprocess.run([sys.executable, IMPORT, '--repo', str(self.copy), '--home', str(self.live),
                               '--patterns', str(self.patterns), *args], capture_output=True, text=True, env=self.env)

    def test_private_or_secret_content_is_not_written(self):
        original = (self.copy / self.config).read_text()
        for text in ('font-size = 10\n# notes for acme-secret-project\n',
                     'font-size = 10\n# token = "sk-ant-' + 'a1b2c3d4e5' * 4 + '"\n',
                     'font-size = 10\n# mail me: person@private-mail.org\n'):
            (self.live / '.config/ghostty/config').write_text(text)
            result = self.run_import('--write', self.config)
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn('not written', result.stdout)
            self.assertEqual((self.copy / self.config).read_text(), original)

    def test_clean_content_is_written(self):
        (self.live / '.config/ghostty').mkdir(parents=True, exist_ok=True)
        (self.live / '.config/ghostty/config').write_text('font-size = 11\n')
        result = self.run_import('--write', self.config)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual((self.copy / self.config).read_text(), 'font-size = 11\n')


if __name__ == '__main__':
    unittest.main(verbosity=2)
