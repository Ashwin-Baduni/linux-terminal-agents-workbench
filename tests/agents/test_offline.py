#!/usr/bin/env python3
"""Offline tests for agent-pair: real tmux, real relay and hooks, fake agents, no tokens.

Run:  tests/run.py relay        (several minutes; or run this file directly)
"""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import support  # noqa: E402

HERE = Path(__file__).resolve().parent
STAGE = support.home()
PAIR = STAGE / '.local/bin/agent-pair'
FAKE = HERE / 'fake_agent.py'
WORKSPACE_HOOK = str(STAGE / '.local/bin/workspace-control') + ' hook'
CONF = STAGE / '.config/tmux/workspace.conf'


def load_pair_module():
    loader = importlib.machinery.SourceFileLoader('agent_pair', str(PAIR))
    spec = importlib.util.spec_from_loader('agent_pair', loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class PairTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='agent-pair-offline-'))
        self.project = self.tmp / 'project'
        self.project.mkdir()
        self.socket = 'agent-pair-offline-' + uuid.uuid4().hex[:8]
        quiet = self.tmp / 'bin'  # A silent notify-send keeps test runs off the desktop.
        quiet.mkdir()
        (quiet / 'notify-send').write_text('#!/bin/sh\nexit 0\n')
        for name in ('claude', 'codex'):  # Launched fakes show up as the real agent names do.
            (quiet / name).write_text(f'#!/bin/bash\nexec -a {name} {sys.executable} {FAKE} "$@"\n')
        for path in quiet.iterdir():
            path.chmod(0o755)
        self.env = dict(os.environ, WORKSPACE_TMUX_SOCKET=self.socket, AGENT_PAIR_STATE=str(self.tmp / 'state'),
                        AGENT_PAIR_CLAUDE_BIN=str(quiet / 'claude'), AGENT_PAIR_CODEX_BIN=str(quiet / 'codex'),
                        PATH=str(quiet) + ':' + os.environ['PATH'])
        for name in ('TMUX', 'TMUX_PANE'):
            self.env.pop(name, None)

    def tearDown(self):
        subprocess.run(['tmux', '-L', self.socket, 'kill-server'], capture_output=True)
        # tmux leaves the socket file behind; remove it so test runs don't pile them up.
        Path(os.environ.get('TMUX_TMPDIR') or '/tmp', f'tmux-{os.getuid()}', self.socket).unlink(missing_ok=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def start(self, plan=None, task='Build the thing.', orchestrator='codex:gpt-6-astra', coder='claude:claude-opus-5-5', **env):
        env = dict(self.env, FAKE_PLAN=json.dumps(plan or {}), **env)
        result = subprocess.run([PAIR, 'start', '--detached', '--folder', self.project, '--orchestrator', orchestrator,
                                 '--coder', coder, '--task', task],
                                env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.pair = Path(result.stdout.strip())
        return self.pair

    def status(self):
        try:
            return json.loads((self.pair / 'status.json').read_text())
        except (OSError, ValueError):
            return {}

    def wait_for(self, check, timeout=30, what='condition'):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.status()
            try:
                if check(status):
                    return status
            except KeyError:
                pass  # The relay has not written that field yet.
            time.sleep(0.3)
        log = (self.pair / 'relay.log').read_text() if (self.pair / 'relay.log').exists() else ''
        self.fail(f'Timed out waiting for {what}. Status: {self.status()}\nRelay log:\n{log}')

    def prompts(self, role, kind='prompt'):
        path = self.pair / f'fake-{role}.jsonl'
        if not path.exists():
            return []
        return [json.loads(line)['text'] for line in path.read_text().splitlines() if json.loads(line)['kind'] == kind]

    def panes(self):
        return json.loads((self.pair / 'config.json').read_text())['panes']

    def type_into(self, role, text):
        pane = self.panes()[role]
        subprocess.run(['tmux', '-L', self.socket, 'send-keys', '-t', pane, '-l', text], check=True)
        subprocess.run(['tmux', '-L', self.socket, 'send-keys', '-t', pane, 'Enter'], check=True)

    def conversation(self):
        return (self.pair / 'conversation.md').read_text()


class Relay(PairTest):
    def test_full_loop_finishes_on_pair_done(self):
        self.start()
        status = self.wait_for(lambda s: s.get('reason') == 'done', what='PAIR-DONE')
        self.assertEqual(status['relayed'], {'orchestrator': 2, 'coder': 2})
        self.assertEqual(status['round'], 2)
        coder = self.prompts('coder')
        self.assertTrue(coder[0].startswith('[relay → coder · setup]'))
        self.assertTrue(coder[1].startswith('[orchestrator → coder]\n\n'))
        self.assertIn('Step 1: create a.txt', coder[1])
        orchestrator = self.prompts('orchestrator')
        self.assertTrue(orchestrator[0].startswith('[relay → orchestrator · task from the human]'))
        self.assertTrue(orchestrator[2].startswith('[coder → orchestrator]\n\n'))
        self.assertEqual((status['mode'], status['reason']), ('stopped', 'done'))
        self.assertIn('Done: Step 2: create b.txt', orchestrator[2])
        self.assertTrue((self.project / 'coder-step-2.txt').exists())
        conversation = self.conversation()
        self.assertEqual(conversation.count('Orchestrator → Coder · round'), 2)
        self.assertEqual(conversation.count('Coder → Orchestrator · report on round'), 2)
        self.assertEqual(conversation.count('Orchestrator · done'), 1)
        self.assertNotIn('READY', conversation)  # The coder's handshake is never forwarded.

    def test_after_pair_done_nothing_is_forwarded(self):
        self.start()
        self.wait_for(lambda s: s.get('reason') == 'done', what='PAIR-DONE')
        time.sleep(1)
        before = len(self.prompts('coder'))
        self.type_into('orchestrator', 'Good morning. What did you do overnight?')
        time.sleep(3)
        self.assertEqual(len(self.prompts('coder')), before)  # Morning chat stays between you and the agent.
        self.assertEqual(self.status()['mode'], 'stopped')
        tmux = ['tmux', '-L', self.socket, 'display-message', '-p', '-t']
        for pane in self.panes().values():
            self.assertEqual(subprocess.run(tmux + [pane, '#{@pair_dir}'], capture_output=True, text=True).stdout.strip(), '')

    def test_pause_holds_replies_and_resume_delivers_them(self):
        self.start({'orchestrator': ['Create alpha.txt please.', 'Now write the beta notes file.',
                                     'Check the gamma output carefully.'], 'delay': 2.5})
        self.wait_for(lambda s: s['relayed']['orchestrator'] >= 1, what='first instruction')
        subprocess.run([PAIR, 'pause', self.pair], env=self.env, check=True)
        self.wait_for(lambda s: s.get('mode') == 'paused', what='pause')
        held = self.wait_for(lambda s: s.get('held', 0) >= 1, timeout=15, what='a held reply')
        relayed = dict(held['relayed'])
        time.sleep(2)
        self.assertEqual(self.status()['relayed'], relayed)  # Nothing moves while paused.
        subprocess.run([PAIR, 'resume', self.pair], env=self.env, check=True)
        self.wait_for(lambda s: sum(s['relayed'].values()) > sum(relayed.values()), what='delivery after resume')

    def test_long_message_travels_as_a_file(self):
        long_reply = 'Implement this spec.\n' + 'x' * 25000
        self.start({'orchestrator': [long_reply, 'Looks good.\nPAIR-DONE']})
        self.wait_for(lambda s: s.get('reason') == 'done', what='PAIR-DONE')
        self.assertIn('saved to a file', self.prompts('coder')[1])
        saved = list((self.pair / 'messages').glob('*-instruction-round-1.md'))
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].read_text(), long_reply)

    def test_relay_presses_enter_again_when_the_first_is_lost(self):
        self.start({'ignore_first_enter': True})
        self.wait_for(lambda s: s.get('reason') == 'done', timeout=60, what='PAIR-DONE despite a lost Enter')

    def test_agent_exit_stops_the_relay(self):
        self.start({'exit_after': {'coder': 2}, 'orchestrator': ['Create alpha.txt please.', 'Now write the beta notes.']})
        status = self.wait_for(lambda s: s.get('mode') == 'stopped', what='relay stop on exit')
        self.assertEqual(status['reason'], 'coder exited')

    def test_same_vendor_pairs_work(self):
        for orchestrator, coder in (('codex:gpt-6-astra', 'codex:gpt-6-luna'),
                                    ('claude:claude-fable-5-1', 'claude:claude-opus-5-5')):
            with self.subTest(orchestrator=orchestrator, coder=coder):
                self.start(orchestrator=orchestrator, coder=coder)
                self.wait_for(lambda s: s.get('reason') == 'done', what='PAIR-DONE')


class StayingInSync(PairTest):
    """One message in flight: every report answers the instruction before it, whatever the human does."""

    def test_orchestrator_waits_for_the_report_and_gets_its_held_reply_back(self):
        self.start({'orchestrator': ['Step 1: create a.txt', 'Status: the coder is busy.', 'All verified.\nPAIR-DONE'],
                    'delay': 3})
        self.wait_for(lambda s: s['relayed']['orchestrator'] >= 1, what='first instruction')
        self.type_into('orchestrator', 'How is it going?')
        status = self.wait_for(lambda s: s.get('reason') == 'done', timeout=60, what='PAIR-DONE')
        self.assertEqual(status['relayed'], {'orchestrator': 1, 'coder': 1})
        self.assertEqual(len(self.prompts('coder')), 2)  # Handshake and the one instruction: no second message in flight.
        report = self.prompts('orchestrator')[-1]
        self.assertTrue(report.startswith('[coder → orchestrator]'))
        self.assertIn('Status: the coder is busy.', report)
        self.assertIn('It was not sent to the coder', report)
        self.assertIn('held until the report on round 1', self.conversation())

    def test_coder_answers_to_you_stay_private(self):
        self.start({'orchestrator': ['Step 1: create a.txt', 'Step 2: write the notes file.', 'All verified.\nPAIR-DONE'],
                    'delay': 1})
        self.wait_for(lambda s: s['relayed']['coder'] >= 1, what='first report')
        self.type_into('coder', 'Quick question from the human')
        status = self.wait_for(lambda s: s.get('reason') == 'done', timeout=60, what='PAIR-DONE')
        self.assertEqual(status['relayed'], {'orchestrator': 2, 'coder': 2})
        self.assertFalse(any('Quick question' in prompt for prompt in self.prompts('orchestrator')))
        self.assertIn('Coder → You (not forwarded)', self.conversation())
        self.assertTrue(self.prompts('orchestrator')[2].startswith('[coder → orchestrator]'))

    def test_an_interrupted_turn_can_be_skipped(self):
        self.start({'no_stop': {'coder': 2}, 'orchestrator': ['Step 1: create a.txt', 'Try another way.', 'All verified.\nPAIR-DONE']})
        self.wait_for(lambda s: s['relayed']['orchestrator'] >= 1 and s.get('open_round') == 1, what='first instruction')
        time.sleep(2)
        self.assertEqual(self.status()['ball'], 'coder')  # No report came back: the pair would wait forever.
        subprocess.run([PAIR, 'skip', self.pair], env=self.env, check=True)
        self.wait_for(lambda s: s['relayed']['orchestrator'] >= 2, what='next instruction after skipping')
        note = next(p for p in self.prompts('orchestrator') if p.startswith('[relay → orchestrator]'))
        self.assertIn('ended without a report', note)

    def test_an_empty_report_still_returns_the_turn(self):
        self.start({'coder': '', 'orchestrator': ['Step 1: create a.txt', 'All verified.\nPAIR-DONE']})
        self.wait_for(lambda s: s.get('reason') == 'done', what='PAIR-DONE after an empty report')
        self.assertIn('without a written report', self.prompts('orchestrator')[1])


class Stalls(PairTest):
    """Overnight nobody presses Esc or says "carry on": the relay does, then hands a second stall to the orchestrator."""

    def test_a_frozen_turn_is_stopped_and_sent_back_to_work(self):
        self.start({'hang': {'coder': [2]}}, AGENT_PAIR_STUCK_SECONDS='4')
        self.wait_for(lambda s: s.get('reason') == 'done', timeout=60, what='PAIR-DONE after a frozen turn')
        note = next(p for p in self.prompts('coder') if p.startswith('[relay → coder]'))
        self.assertIn('nothing changed on your screen for 4 seconds', note)
        self.assertIn('report what is blocking you to the orchestrator', note)
        self.assertIn('stopping that step', (self.pair / 'relay.log').read_text())

    def test_a_turn_that_ended_without_a_report_gets_a_note(self):
        self.start({'no_stop': {'coder': 2}}, AGENT_PAIR_ENDED_SECONDS='3')
        self.wait_for(lambda s: s.get('reason') == 'done', timeout=60, what='PAIR-DONE after a silent turn')
        self.assertTrue(any('ended without a report' in p for p in self.prompts('coder')))
        self.assertEqual(self.status()['relayed']['coder'], 2)  # The reply to the note became the report.

    def test_stalling_twice_on_one_step_goes_to_the_orchestrator(self):
        self.start({'hang': {'coder': [2, 3]}, 'orchestrator': ['Step 1: create a.txt', 'Try a smaller step.',
                                                                  'All verified.\nPAIR-DONE']},
                   AGENT_PAIR_STUCK_SECONDS='4')
        self.wait_for(lambda s: s.get('reason') == 'done', timeout=90, what='PAIR-DONE after two stalls')
        note = next(p for p in self.prompts('orchestrator') if p.startswith('[relay → orchestrator]'))
        self.assertIn('stuck twice on this step', note)
        self.assertIn('Try a smaller step.', self.prompts('coder')[-1])


class RunawayChecks(PairTest):
    """No round limit. A quiet reminder now and then; loops and idle rounds get a note first, a pause only after it."""

    def test_reminder_rides_along_every_n_reports(self):
        steps = ['Audit the parser', 'Benchmark startup time', 'Check licence headers', 'Document the CLI flags',
                 'All verified.\nPAIR-DONE']
        self.start({'orchestrator': steps}, AGENT_PAIR_REMINDER_EVERY='2')
        self.wait_for(lambda s: s.get('reason') == 'done', timeout=60, what='PAIR-DONE')
        reports = self.prompts('orchestrator')[1:]
        self.assertEqual(['PAIR-DONE ends the relay' in r for r in reports], [False, True, False, True])

    def test_a_loop_gets_a_note_then_a_pause(self):
        self.start({'orchestrator': ['Please run the tests again.']})
        status = self.wait_for(lambda s: s.get('reason') == 'loop', timeout=90, what='pause after the loop note')
        self.assertTrue(any('look almost the same' in p for p in self.prompts('orchestrator')))
        self.assertLessEqual(status['relayed']['orchestrator'], 8)

    def test_rounds_without_file_changes_get_a_note_then_a_pause(self):
        topics = ['Audit the parser', 'Benchmark startup time', 'Check licence headers', 'Document the CLI flags',
                  'Explore cache misses', 'Fix flaky timeouts', 'Gather crash reports', 'Harden input checks',
                  'Inspect memory growth', 'Justify the defaults']
        self.start({'orchestrator': topics, 'coder': '{first_line}', 'no_files': True}, AGENT_PAIR_QUIET_ROUNDS='3')
        status = self.wait_for(lambda s: s.get('reason') == 'no progress', timeout=90, what='pause after the quiet note')
        self.assertTrue(any('reports changed no files' in p for p in self.prompts('orchestrator')))
        self.assertEqual(status['relayed']['orchestrator'], 6)


class Joined(PairTest):
    """Helpers for pairing agents that are already running in a window."""

    def tmux(self, *args):
        return subprocess.run(['tmux', '-L', self.socket, *args], capture_output=True, text=True, check=True).stdout.strip()

    def running(self, role, wrapped=False):
        """A fake Claude reporting through the workspace hook, as a real running Claude does."""
        hooks = {event: [{'hooks': [{'type': 'command', 'command': WORKSPACE_HOOK}]}] for event in ('UserPromptSubmit', 'Stop')}
        agent = f'FAKE_ROLE={role} exec -a claude {sys.executable} {FAKE} --settings {shlex.quote(json.dumps({"hooks": hooks}))}'
        if not wrapped:
            return 'bash -c ' + shlex.quote(agent)
        # Like the Agents… menu: a Python wrapper runs the agent, then the pane returns to a shell.
        script = (f'import os, subprocess; subprocess.run(["bash", "-c", {agent!r}]); '
                  'os.execvp("bash", ["bash", "--norc", "--noprofile"])')
        return shlex.join([sys.executable, '-c', script])

    def window(self, plan, *commands, **extra):
        env = dict(self.env, FAKE_PLAN=json.dumps(plan), FAKE_LOG=str(self.tmp), **extra)
        first = subprocess.run(['tmux', '-L', self.socket, '-f', CONF, 'new-session', '-d', '-P', '-F', '#{pane_id}',
                                '-s', 'work', '-x', '220', '-y', '50', '-c', self.project, commands[0]],
                               env=env, capture_output=True, text=True, check=True).stdout.strip()
        panes = [first] + [self.tmux('split-window', '-h', '-P', '-F', '#{pane_id}', '-t', first, '-c', self.project, c)
                           for c in commands[1:]]
        time.sleep(1.5)
        return panes

    def join(self, *args):
        result = subprocess.run([PAIR, 'join', *args], env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.pair = Path(result.stdout.strip())
        return self.pair

    def log(self, role, kind):
        path = self.tmp / f'fake-{role}.jsonl'
        rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [row['text'] for row in rows if row['kind'] == kind]

    def say(self, pane, text):
        self.tmux('send-keys', '-t', pane, '-l', text)
        self.tmux('send-keys', '-t', pane, 'Enter')

    def give_task(self, pane, text='Please create a.txt.'):
        """Joined agents wait for you: both say READY, then you give the orchestrator the work."""
        self.wait_for(lambda s: s['orchestrator_ready'] and s['coder_ready'], what='both READY')
        self.assertEqual(self.status()['relayed'], {'orchestrator': 0, 'coder': 0})  # READY is never forwarded.
        self.say(pane, text)

    def option(self, pane, name):
        return self.tmux('display-message', '-p', '-t', pane, '#{' + name + '}')


class Join(Joined):
    """Agents that were already running in a window are paired without restarting them."""

    def test_two_running_agents_pair_and_earlier_chat_stays_private(self):
        plan = {'orchestrator': ['Noted, waiting for the go.', '(brief)', 'Step 1: create a.txt', 'All verified.\nPAIR-DONE']}
        orchestrator, coder = self.window(plan, self.running('orchestrator'), self.running('coder'))
        self.say(orchestrator, 'We will build a.txt soon.')
        deadline = time.monotonic() + 10
        while not self.log('orchestrator', 'reply') and time.monotonic() < deadline:
            time.sleep(0.2)
        self.join('--orchestrator-pane', orchestrator, '--coder-pane', coder)
        self.give_task(orchestrator)
        status = self.wait_for(lambda s: s.get('reason') == 'done', what='PAIR-DONE')
        self.assertEqual(status['relayed'], {'orchestrator': 1, 'coder': 1})
        coder_prompts = self.log('coder', 'prompt')
        self.assertTrue(coder_prompts[0].startswith('[relay → coder · setup · ') and 'READY' in coder_prompts[0])
        self.assertIn('Step 1: create a.txt', coder_prompts[1])
        self.assertFalse(any('Noted, waiting' in prompt for prompt in coder_prompts))
        brief = self.log('orchestrator', 'prompt')[1]
        self.assertTrue(brief.startswith('[relay → orchestrator · setup · '))
        self.assertTrue(brief.endswith('Reply READY. The task comes next.'))
        deadline = time.monotonic() + 5
        while self.option(orchestrator, '@pair_dir') and time.monotonic() < deadline:
            time.sleep(0.2)
        self.assertEqual([self.option(p, '@pair_dir') for p in (orchestrator, coder)], ['', ''])  # Released when done.
        relay = json.loads((self.pair / 'config.json').read_text())['relay_pane']
        self.assertEqual(self.option(relay, 'pane_width'), self.option(relay, 'window_width'))  # Bottom strip by default.
        self.assertEqual(self.option(relay, '@workspace_status'), 'Finished')

    def test_a_busy_agent_finishes_its_own_turn_before_joining(self):
        plan = {'orchestrator': ['Pre-pair answer.', '(brief)', 'Step 1: create a.txt', 'All verified.\nPAIR-DONE'], 'delay': 3}
        orchestrator, coder = self.window(plan, self.running('orchestrator'), self.running('coder'))
        self.say(orchestrator, 'Think about this first.')
        time.sleep(1)
        self.join('--orchestrator-pane', orchestrator, '--coder-pane', coder)
        self.give_task(orchestrator)
        self.wait_for(lambda s: s.get('reason') == 'done', timeout=60, what='PAIR-DONE')
        self.assertFalse(any('Pre-pair answer.' in prompt for prompt in self.log('coder', 'prompt')))
        self.assertTrue(self.log('orchestrator', 'prompt')[1].startswith('[relay → orchestrator · setup · '))
        self.assertIn('from before the pair', (self.pair / 'relay.log').read_text())

    def test_one_running_agent_gets_a_new_coder_in_the_idle_shell(self):
        plan = {'orchestrator': ['(brief)', 'Step 1: create a.txt', 'All verified.\nPAIR-DONE']}
        orchestrator, shell = self.window(plan, self.running('orchestrator'), 'bash --norc --noprofile')
        self.join('--orchestrator-pane', orchestrator, '--coder', 'claude:claude-opus-5-5')
        self.give_task(orchestrator)
        self.wait_for(lambda s: s.get('reason') == 'done', what='PAIR-DONE')
        self.assertEqual(json.loads((self.pair / 'config.json').read_text())['panes']['coder'], shell)
        coder = self.log('coder', 'prompt')
        self.assertTrue(coder[0].startswith('[relay → coder · setup]'))
        self.assertIn('Step 1: create a.txt', coder[1])

    def test_relay_never_types_into_a_shell(self):
        plan = {'orchestrator': ['(brief)', 'Create alpha.txt please.', 'Now write the beta notes.'], 'exit_after': {'coder': 2}}
        orchestrator, coder = self.window(plan, self.running('orchestrator'), self.running('coder', wrapped=True))
        self.assertEqual(self.option(coder, 'pane_current_command'), 'python3')  # Pairing looks underneath the wrapper.
        self.join('--orchestrator-pane', orchestrator, '--coder-pane', coder)
        self.give_task(orchestrator)
        status = self.wait_for(lambda s: s.get('mode') == 'stopped', timeout=40, what='relay stop')
        self.assertEqual(status['reason'], 'coder is not running')
        self.assertNotIn('→ coder]', self.tmux('capture-pane', '-p', '-t', coder))
        time.sleep(1)
        self.assertEqual(self.option(orchestrator, '@pair_role'), '')  # Released on stop.

    def test_unpair_keeps_both_agents_and_their_names(self):
        plan = {'orchestrator': ['(brief)', 'Create alpha.txt please.', 'Now write the beta notes.']}
        orchestrator, coder = self.window(plan, self.running('orchestrator'), self.running('coder'))
        self.tmux('set-option', '-p', '-t', orchestrator, '@name', 'Agent 1')
        self.tmux('set-option', '-p', '-t', coder, '@name', 'Agent 2')
        self.join('--orchestrator-pane', orchestrator, '--coder-pane', coder)
        self.give_task(orchestrator)
        self.wait_for(lambda s: s['relayed']['orchestrator'] >= 1, what='first instruction')
        self.assertEqual((self.option(coder, '@role_label'), self.option(coder, '@name')), ('Coder', 'Agent 2'))
        subprocess.run([PAIR, 'end', self.pair], env=self.env, check=True)
        time.sleep(0.5)
        panes = self.tmux('list-panes', '-t', 'work', '-F', '#{pane_id}').split()
        self.assertEqual(sorted(panes), sorted([orchestrator, coder]))
        self.assertEqual([self.option(p, '@name') for p in (orchestrator, coder)], ['Agent 1', 'Agent 2'])
        self.assertEqual([self.option(p, '@pair_dir') for p in (orchestrator, coder)], ['', ''])
        self.assertEqual([self.option(p, '@role_label') for p in (orchestrator, coder)], ['', ''])
        self.assertEqual(self.option(coder, 'pane_current_command'), 'claude')

    def test_the_same_agents_pair_again_after_pair_done(self):
        plan = {'orchestrator': ['Step 1: create a.txt', 'All verified.\nPAIR-DONE', '(brief)',
                                 'Step 2: create b.txt', 'All verified again.\nPAIR-DONE']}
        self.start(plan)
        self.wait_for(lambda s: s.get('reason') == 'done', what='first PAIR-DONE')
        first, panes = self.pair, self.panes()
        time.sleep(1)
        first_events = len(list((first / 'events' / 'done').glob('*.json')))
        self.join('--orchestrator-pane', panes['orchestrator'], '--coder-pane', panes['coder'])
        self.assertNotEqual(self.pair, first)
        self.wait_for(lambda s: s['orchestrator_ready'] and s['coder_ready'], what='both READY again')
        self.say(panes['orchestrator'], 'Next: create b.txt.')
        status = self.wait_for(lambda s: s.get('reason') == 'done', what='second PAIR-DONE')
        self.assertEqual(status['relayed'], {'orchestrator': 1, 'coder': 1})
        self.assertEqual(len(list((first / 'events' / 'done').glob('*.json'))), first_events)  # The old pair hears nothing.
        self.assertTrue(json.loads((self.pair / 'config.json').read_text())['roles']['coder']['hooked'])

    def test_relay_runs_under_all_panes_and_can_move_left(self):
        orchestrator, coder = self.window({'delay': 1}, self.running('orchestrator'), self.running('coder'))
        directory = self.tmux('split-window', '-hbf', '-l', '24%', '-P', '-F', '#{pane_id}', '-t', orchestrator, 'sleep 600')
        self.tmux('set-option', '-p', '-t', directory, '@name', 'Directory')
        self.join('--orchestrator-pane', orchestrator, '--coder-pane', coder)
        relay = json.loads((self.pair / 'config.json').read_text())['relay_pane']

        def along_the_bottom():
            self.assertEqual(self.option(relay, 'pane_width'), self.option(relay, 'window_width'))
            self.assertEqual(self.option(relay, 'pane_bottom'), str(int(self.option(relay, 'window_height')) - 1))
            for pane in (directory, orchestrator, coder):
                self.assertLess(int(self.option(pane, 'pane_bottom')), int(self.option(relay, 'pane_top')))

        along_the_bottom()
        subprocess.run([PAIR, 'move-relay', self.pair, 'left'], env=self.env, check=True)
        self.assertEqual(self.option(relay, 'pane_left'), self.option(directory, 'pane_left'))
        self.assertLess(int(self.option(relay, 'pane_top')), int(self.option(directory, 'pane_top')))
        self.assertEqual(json.loads((self.tmp / 'state' / 'last.json').read_text())['relay_position'], 'left')
        subprocess.run([PAIR, 'move-relay', self.pair, 'bottom'], env=self.env, check=True)
        along_the_bottom()


class CodexJoin(unittest.TestCase):
    """A joined Codex has no pair hooks, so its turns are read from Codex's shared server."""

    class Api:
        def __init__(self, turns):
            self.turns = turns

        def request(self, method, params):
            if method == 'thread/list':
                return {'data': [{'id': 'other', 'updatedAt': time.time()}, {'id': 'mine', 'updatedAt': time.time()}]}
            return {'data': sorted(self.turns.get(params['threadId'], []), key=lambda t: -t['startedAt'])[:params['limit']]}

        def close(self):
            pass

    def test_turns_become_events_and_older_turns_are_skipped(self):
        pair = load_pair_module()
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)

        def user(text):
            return {'type': 'userMessage', 'content': [{'type': 'text', 'text': text}]}

        def answer(text, phase='final_answer'):
            return {'type': 'agentMessage', 'text': text, 'phase': phase}

        turns = {'other': [{'id': 'x', 'startedAt': 1, 'status': 'completed', 'items': [user('unrelated')]}],
                 'mine': [{'id': f'old{n}', 'startedAt': n / 10, 'status': 'completed',
                           'items': [user(f'earlier chat {n}'), answer(f'old answer {n}')]} for n in range(1, 8)]
                         + [{'id': 'setup', 'startedAt': 2, 'status': 'inProgress', 'items': [user('[relay → coder · setup · T:coder] hi')]}]}
        relay = type('Relay', (), {'dir': directory, 'setup_sent': {'coder': True}, 'say': lambda *a, **k: None,
                                   'config': {'roles': {'coder': {'token': 'T:coder'}}}, 'status': {}})()
        watch = pair.CodexWatch(relay, 'coder')
        api = self.Api(turns)
        pair.codex_server = lambda: api
        watch.poll()

        def events():
            return [json.loads(p.read_text()) for p in sorted((directory / 'events').glob('*.json'))]

        self.assertEqual([e['hook_event_name'] for e in events()], ['UserPromptSubmit'])
        self.assertIn('T:coder', events()[0]['prompt'])
        turns['mine'][-1].update(status='completed', items=[user('[relay → coder · setup · T:coder] hi'),
                                                            answer('thinking', 'commentary'), answer('READY')])
        for _ in range(3):  # Later polls read further back than the first one did.
            watch.next = 0
            watch.poll()
        self.assertEqual(len(events()), 2)
        turns['mine'].append({'id': 'next', 'startedAt': 3, 'status': 'interrupted', 'items': [user('[orchestrator → coder] go')]})
        watch.next = 0
        watch.poll()
        names = [(e['hook_event_name'], e.get('last_assistant_message')) for e in events()]
        # Esc is an Interrupt, never an empty reply that would count as a report.
        self.assertEqual(names, [('UserPromptSubmit', None), ('Stop', 'READY'), ('UserPromptSubmit', None), ('Interrupt', None)])
        heartbeat = directory / 'heartbeat-coder'
        heartbeat.unlink(missing_ok=True)
        working = {'id': 'busy', 'startedAt': 4, 'status': 'inProgress', 'items': [user('[orchestrator → coder] next')]}
        turns['mine'].append(working)
        watch.next = 0
        watch.poll()
        self.assertTrue(heartbeat.exists())  # A turn that moves is a sign of life.
        heartbeat.unlink()
        watch.next = 0
        watch.poll()
        self.assertFalse(heartbeat.exists())  # Nothing new: no heartbeat.
        working['items'] = working['items'] + [{'type': 'contextCompaction', 'id': 'c1'}, answer('more', 'commentary')]
        watch.next = 0
        watch.poll()
        self.assertTrue(heartbeat.exists())
        working.update(status='failed', error={'message': "You've hit your usage limit.", 'codexErrorInfo': 'usageLimitExceeded',
                                               'additionalDetails': None})
        watch.next = 0
        watch.poll()
        watch.next = 0
        watch.poll()  # Each change is reported once.
        tail = [(e['hook_event_name'], e.get('source') or e.get('error')) for e in events()[4:]]
        self.assertEqual(tail, [('UserPromptSubmit', None), ('SessionStart', 'compact'), ('StopFailure', 'usageLimitExceeded')])
        self.assertEqual(events()[-1]['error_details'], "You've hit your usage limit.")
        self.assertEqual(watch.thread, 'mine')
        # Remembered, so a restarted relay keeps reading the same conversation from the same point.
        again = pair.CodexWatch(relay, 'coder')
        self.assertEqual((again.thread, again.start), ('mine', 2))


class Handshake(unittest.TestCase):
    """Only the turn that answered the brief is the READY; a message of the human's never takes its place."""

    def test_an_earlier_turn_is_not_mistaken_for_ready(self):
        pair = load_pair_module()
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        (directory / 'config.json').write_text(json.dumps({
            'folder': str(directory), 'panes': {'orchestrator': '%0', 'coder': '%1'},
            'roles': {'orchestrator': {'agent': 'claude', 'label': 'Claude', 'handshake': True},
                      'coder': {'agent': 'claude', 'label': 'Claude'}}}))
        relay = pair.Relay(directory)
        relay.status['coder_ready'] = True
        brief = '[relay → orchestrator · setup · T:orchestrator]\n\nA relay connects you with a coding agent'
        relay.pending['orchestrator'] = {'since': 0, 'retried': False, 'item': {}, 'via': 'paste', 'text': brief,
                                         'signature': pair.signature(brief)}  # The brief was just pasted.
        relay.prompt('orchestrator', 'ok go ahead with the old task')
        self.assertIn('orchestrator', relay.pending)  # Still waiting for the brief's own turn.
        relay.reply('orchestrator', 'Implemented throughout the site.')
        self.assertFalse(relay.status['orchestrator_ready'])
        relay.prompt('orchestrator', '  <pasted_content id="1"> [relay → orchestrator · setup · T:orchestrator]\n\nA relay '
                                     'connects you with a coding agent </pasted_content id="1">')
        relay.reply('orchestrator', 'READY')
        self.assertTrue(relay.status['orchestrator_ready'])
        self.assertEqual(relay.queue['coder'], [])  # READY is never forwarded.
        relay.prompt('orchestrator', 'Please build the thing.')
        relay.reply('orchestrator', 'Step 1: create a.txt')
        self.assertEqual([item['text'] for item in relay.queue['coder']], ['Step 1: create a.txt'])

    def test_busy_screens(self):
        pair = load_pair_module()
        busy = ['✢ Scampering… (27m 59s · ↓ 77.0k tokens)\n❯\n  ⏵⏵ bypass permissions on · 1 shell · esc to…',
                '• Working (3s • esc to interrupt)\n› Ask Codex to do anything']
        idle = ['✻ Churned for 13s · done 8:40 PM\n❯\n  ⏵⏵ bypass permissions on · ← for agents',
                '• PONG\n  Worked for 3s • 6:16 PM\n› Ask Codex to do anything']
        self.assertTrue(all(pair.BUSY_SCREEN.search(screen) for screen in busy))
        self.assertFalse(any(pair.BUSY_SCREEN.search(screen) for screen in idle))


class OwnMessages(unittest.TestCase):
    """Claude's own notices (a background command finishing) are not the human; the human's are."""

    def make(self):
        os.environ['AGENT_PAIR_ENDED_SECONDS'] = '1'
        self.addCleanup(os.environ.pop, 'AGENT_PAIR_ENDED_SECONDS', None)
        pair = load_pair_module()
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        (directory / 'config.json').write_text(json.dumps({
            'folder': str(directory), 'panes': {'orchestrator': '%0', 'coder': '%1'},
            'roles': {'orchestrator': {'agent': 'codex', 'label': 'Codex'}, 'coder': {'agent': 'claude', 'label': 'Claude'}}}))
        relay = pair.Relay(directory)
        relay.status.update(coder_ready=True, orchestrator_ready=True, ball='coder', open_round=1, round=1)
        relay.side = {'orchestrator': 'idle', 'coder': 'idle'}
        return pair, relay, directory

    NOTICE = '<task-notification>\n<task-id>bvjunj2jp</task-id>\n<status>completed</status>\n</task-notification>'

    def test_background_notices_continue_the_task(self):
        pair, relay, directory = self.make()
        relay.prompt('coder', '\n\n<pasted_content id="7d09">\n[orchestrator → coder]\n\nBuild the row.')
        relay.prompt('coder', self.NOTICE)  # Mid-task: part of the same turn.
        relay.reply('coder', 'Built the row; checks pass.')
        self.assertEqual([item['text'] for item in relay.queue['orchestrator']], ['Built the row; checks pass.'])
        relay.prompt('coder', self.NOTICE)  # After the report: the coder's own follow-up goes nowhere.
        relay.reply('coder', 'The background comparison also finished.')
        self.assertEqual(len(relay.queue['orchestrator']), 1)
        log = directory / 'relay.log'
        self.assertNotIn('You messaged', log.read_text() if log.exists() else '')

    def test_a_notice_that_starts_a_turn_while_a_report_is_owed_leads_to_the_report(self):
        pair, relay, directory = self.make()
        relay.prompt('coder', self.NOTICE)
        relay.reply('coder', 'All done: final report.')
        self.assertEqual([item['text'] for item in relay.queue['orchestrator']], ['All done: final report.'])

    def test_your_message_mid_task_stays_private_then_the_coder_is_reminded(self):
        pair, relay, directory = self.make()
        relay.prompt('coder', '[orchestrator → coder]\n\nBuild the row.')
        relay.prompt('coder', 'u done?')  # Typed after Esc, while the relay's task was still running.
        relay.reply('coder', 'Not yet. Should I carry on?')
        self.assertEqual(relay.queue['orchestrator'], [])  # The answer to you is not sent on.
        self.assertIn('You messaged the coder', (directory / 'relay.log').read_text())
        time.sleep(1.2)
        relay.watched = 0
        relay.watchdog()
        self.assertEqual(relay.queue['coder'][0], {'kind': 'nudge', 'text': pair.ENDED_NOTE['coder']})


class Hook(unittest.TestCase):
    def run_hook(self, env, payload):
        return subprocess.run([PAIR, 'hook'], input=payload, env=env, capture_output=True, text=True, timeout=10)

    def test_silent_outside_a_pair(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith('AGENT_PAIR')}
        result = self.run_hook(env, json.dumps({'hook_event_name': 'Stop', 'last_assistant_message': 'hi'}))
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))

    def test_bad_input_never_blocks_the_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {k: v for k, v in os.environ.items() if k not in ('TMUX', 'TMUX_PANE')}  # Not inside any pane.
            env.update(AGENT_PAIR_DIR=directory, AGENT_PAIR_ROLE='coder')
            result = self.run_hook(env, 'not json')
            self.assertEqual((result.returncode, result.stdout), (0, ''))
            result = self.run_hook(env, json.dumps({'hook_event_name': 'Stop', 'last_assistant_message': 'hi'}))
            self.assertEqual((result.returncode, result.stdout), (0, ''))
            events = list(Path(directory, 'events').glob('*.json'))
            self.assertEqual(len(events), 1)
            self.assertEqual(json.loads(events[0].read_text())['last_assistant_message'], 'hi')
            Path(directory, 'status.json').write_text(json.dumps({'mode': 'stopped', 'reason': 'done'}))
            self.run_hook(env, json.dumps({'hook_event_name': 'Stop', 'last_assistant_message': 'later'}))
            self.assertEqual(len(list(Path(directory, 'events').glob('*.json'))), 1)  # A finished pair hears nothing.

    def test_tool_calls_only_touch_the_heartbeat(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {k: v for k, v in os.environ.items() if k not in ('TMUX', 'TMUX_PANE')}
            env.update(AGENT_PAIR_DIR=directory, AGENT_PAIR_ROLE='coder')
            for name in ('PostToolUse', 'PostToolUseFailure'):
                result = self.run_hook(env, json.dumps({'hook_event_name': name, 'tool_name': 'Bash'}))
                self.assertEqual((result.returncode, result.stdout), (0, ''))
            self.assertTrue(Path(directory, 'heartbeat-coder').exists())
            self.assertEqual(list(Path(directory).glob('events/*.json')), [])
            self.run_hook(env, json.dumps({'hook_event_name': 'StopFailure', 'error': 'rate_limit', 'error_details': 'x'}))
            event = json.loads(next(Path(directory, 'events').glob('*.json')).read_text())
            self.assertEqual((event['error'], event['error_details']), ('rate_limit', 'x'))


class Commands(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pair = load_pair_module()

    def test_specs(self):
        self.assertEqual(self.pair.parse_spec('codex:gpt-6-luna:high'),
                         {'agent': 'codex', 'model': 'gpt-6-luna', 'effort': 'high'})
        self.assertEqual(self.pair.parse_spec('claude:claude-fable-5-1')['effort'], '')
        with self.assertRaises(ValueError):
            self.pair.parse_spec('gemini:pro')

    def test_codex_command_is_valid_toml(self):
        spec = self.pair.parse_spec('codex:gpt-6-luna:high')
        args = self.pair.agent_command(spec, 'coder', '/tmp/x "quoted"', 'Claude · Fable 5.1', 'go')
        overrides = [args[i + 1] for i, arg in enumerate(args) if arg == '-c']
        parsed = {}
        for override in overrides:
            key, value = override.split('=', 1)
            parsed[key] = tomllib.loads('v = ' + value)['v']
        for event in ('UserPromptSubmit', 'Stop', 'Interrupt', 'SessionStart', 'SessionEnd', 'PostToolUse'):
            self.assertEqual(parsed['hooks.' + event][0]['hooks'][0]['command'], self.pair.HOOK)
        self.assertIn('/tmp/x "quoted"', parsed['developer_instructions'])
        self.assertEqual(parsed['model_reasoning_effort'], 'high')
        self.assertEqual(args[args.index('-m') + 1], 'gpt-6-luna')
        self.assertEqual(args[-1], 'go')

    def test_claude_command(self):
        spec = self.pair.parse_spec('claude:claude-opus-5-5:max')
        args = self.pair.agent_command(spec, 'orchestrator', '/p', 'Codex · GPT-6-Luna', '')
        settings = json.loads(args[args.index('--settings') + 1])
        self.assertEqual(sorted(settings['hooks']), sorted(self.pair.CLAUDE_HOOKS))
        self.assertIn('StopFailure', settings['hooks'])
        self.assertEqual(args[args.index('--model') + 1], 'claude-opus-5-5')
        self.assertEqual(args[args.index('--effort') + 1], 'max')
        brief = args[args.index('--append-system-prompt') + 1]
        self.assertIn('You are the orchestrator', brief)
        self.assertIn('PAIR-DONE on its own line', brief)
        self.assertNotIn('PAIR-HUMAN', brief)
        self.assertLess(len(brief.split()), 120)  # Short: the human's own instructions decide how to work.

    def test_similarity_and_folder_state(self):
        self.assertTrue(self.pair.similar('Please run the tests again.', 'Please run the tests again!'))
        self.assertFalse(self.pair.similar('Audit the parser', 'Benchmark startup time'))
        with tempfile.TemporaryDirectory() as folder:
            before = self.pair.folder_state(folder)
            self.assertEqual(before, self.pair.folder_state(folder))
            Path(folder, 'a.txt').write_text('x')
            self.assertNotEqual(before, self.pair.folder_state(folder))



class Failures(PairTest):
    """A turn that stops on an API error is tried again on its own; errors waiting cannot fix pause the pair."""

    def notes(self, role):
        return [p for p in self.prompts(role) if p.startswith(f'[relay → {role}]')]

    def test_a_claude_error_is_tried_again(self):
        self.start({'fail': {'coder': {'2': 'overloaded'}}}, AGENT_PAIR_RETRY_SCALE='0.02')
        status = self.wait_for(lambda s: s.get('reason') == 'done', timeout=60, what='PAIR-DONE after an error')
        self.assertEqual(len(self.notes('coder')), 1)
        self.assertIn('your last turn stopped on an error (overloaded)', self.notes('coder')[0])
        self.assertEqual(status['relayed'], {'orchestrator': 2, 'coder': 2})
        self.assertEqual(status['failures'], {})
        self.assertIn('trying again in 1 second.', (self.pair / 'relay.log').read_text())

    def test_a_codex_error_is_read_off_its_screen(self):
        # Codex calls no hook for a failed turn: the relay sees a quiet screen and the "■" error line.
        self.start({'fail': {'coder': {'2': 'rate_limit'}}}, coder='codex:gpt-6-luna',
                   AGENT_PAIR_ENDED_SECONDS='2', AGENT_PAIR_RETRY_SCALE='0.01')
        self.wait_for(lambda s: s.get('reason') == 'done', timeout=60, what='PAIR-DONE after a usage limit')
        self.assertIn('stopped on an error (rate_limit)', self.notes('coder')[0])
        self.assertIn("You've hit your usage limit", self.conversation())
        self.assertNotIn('ended without a report', ' '.join(self.prompts('coder')))

    def test_a_sign_in_error_pauses_until_you_resume(self):
        self.start({'fail': {'coder': {'2': 'authentication_failed'}}})
        status = self.wait_for(lambda s: s.get('reason') == 'error', what='a pause on a sign-in error')
        self.assertEqual(status['mode'], 'waiting')
        time.sleep(2)
        self.assertEqual(len(self.prompts('coder')), 2)  # Nothing is retried while it waits for you.
        subprocess.run([PAIR, 'resume', self.pair], env=self.env, check=True)
        self.wait_for(lambda s: s.get('reason') == 'done', timeout=60, what='PAIR-DONE after resume')
        self.assertEqual(len(self.notes('coder')), 1)

    def test_errors_that_keep_coming_pause_after_three_tries(self):
        errors = {str(n): 'server_error' for n in range(2, 6)}
        self.start({'fail': {'coder': errors}}, AGENT_PAIR_RETRY_SCALE='0.01')
        self.wait_for(lambda s: s.get('reason') == 'error', timeout=40, what='a pause after three retries')
        self.assertEqual(len(self.prompts('coder')), 5)  # The handshake, the instruction and three retries.
        subprocess.run([PAIR, 'resume', self.pair], env=self.env, check=True)
        self.wait_for(lambda s: s.get('reason') == 'done', timeout=60, what='PAIR-DONE after resume')

    def test_your_message_takes_over_from_a_waiting_retry(self):
        self.start({'fail': {'coder': {'2': 'rate_limit'}}}, AGENT_PAIR_ENDED_SECONDS='3')  # The retry would be 5 minutes away.
        self.wait_for(lambda s: 'coder' in s.get('failures', {}), what='the failure')
        self.type_into('coder', 'Are you still there?')
        self.wait_for(lambda s: s.get('reason') == 'done', timeout=60, what='PAIR-DONE after your message')
        notes = ' '.join(self.notes('coder'))
        self.assertNotIn('stopped on an error', notes)
        self.assertIn('ended without a report', notes)  # It still owed its report, so it was reminded.


class Interrupts(PairTest):
    """Codex reports Esc as Interrupt. The relay's own Esc is a stall; yours ends the turn, and a note follows later."""

    def test_codex_confirms_the_relays_esc(self):
        self.start({'hang': {'coder': [2]}}, coder='codex:gpt-6-luna', AGENT_PAIR_STUCK_SECONDS='4')
        self.wait_for(lambda s: s.get('reason') == 'done', timeout=60, what='PAIR-DONE after a frozen Codex turn')
        events = [json.loads(p.read_text()) for p in (self.pair / 'events' / 'done').glob('*.json')]
        self.assertTrue(any(e['hook_event_name'] == 'Interrupt' and e['role'] == 'coder' for e in events))
        self.assertIn('nothing changed on your screen for 4 seconds', self.prompts('coder')[2])

    def test_your_esc_on_codex_gets_a_note_later(self):
        self.start({'hang': {'coder': [2]}}, coder='codex:gpt-6-luna', AGENT_PAIR_ENDED_SECONDS='3')
        self.wait_for(lambda s: s.get('open_round') == 1 and s['sides']['coder'] == 'working', what='the coder at work')
        time.sleep(1)
        subprocess.run(['tmux', '-L', self.socket, 'send-keys', '-t', self.panes()['coder'], 'Escape'], check=True)
        self.wait_for(lambda s: s.get('reason') == 'done', timeout=60, what='PAIR-DONE after your Esc')
        self.assertIn("The coder's turn was interrupted.", (self.pair / 'relay.log').read_text())
        notes = [p for p in self.prompts('coder') if p.startswith('[relay → coder]')]
        self.assertEqual(len(notes), 1)
        self.assertIn('ended without a report', notes[0])  # Not taken for an error: "■ Conversation interrupted".


class Heartbeat(PairTest):
    """Tool calls are a sign of life: a still screen or a missing spinner is not a stall while they keep coming."""

    def test_a_still_screen_with_tool_calls_is_left_alone(self):
        self.start({'work': {'coder': {'2': {'seconds': 8, 'spinner': True}}}}, AGENT_PAIR_STUCK_SECONDS='3')
        self.wait_for(lambda s: s.get('reason') == 'done', timeout=60, what='PAIR-DONE')
        self.assertNotIn('stopping that step', (self.pair / 'relay.log').read_text())
        self.assertFalse(any(p.startswith('[relay → coder]') for p in self.prompts('coder')))

    def test_work_without_a_spinner_gets_no_note(self):
        self.start({'work': {'coder': {'2': {'seconds': 8, 'spinner': False}}}}, AGENT_PAIR_ENDED_SECONDS='3')
        self.wait_for(lambda s: s.get('reason') == 'done', timeout=60, what='PAIR-DONE')
        self.assertNotIn('ended without', (self.pair / 'relay.log').read_text())
        self.assertFalse(any(p.startswith('[relay → coder]') for p in self.prompts('coder')))


class Sessions(PairTest):
    def test_a_cleared_coder_hands_its_step_back(self):
        self.start({'no_stop': {'coder': 2}})
        deadline = time.monotonic() + 20
        while len(self.prompts('coder', 'reply')) < 2 and time.monotonic() < deadline:
            time.sleep(0.2)
        self.assertEqual(len(self.prompts('coder', 'reply')), 2)  # Its report, whose Stop never came.
        self.type_into('coder', '/clear')
        status = self.wait_for(lambda s: s.get('reason') == 'done', timeout=60, what='PAIR-DONE after /clear')
        note = next(p for p in self.prompts('orchestrator') if p.startswith('[relay → orchestrator]'))
        self.assertIn("the coder's conversation was cleared", note)
        self.assertEqual(status['relayed'], {'orchestrator': 2, 'coder': 1})
        self.assertEqual(len([p for p in self.prompts('coder') if '· setup' in p]), 1)  # Its brief is its system prompt.


class JoinedSessions(Joined):
    """A joined Claude reports clear, compact, quitting, errors and tool calls through the workspace hook."""

    def test_a_cleared_joined_agent_is_briefed_again(self):
        plan = {'orchestrator': ['(brief)', 'Step 1: create a.txt', 'All verified.\nPAIR-DONE']}
        orchestrator, coder = self.window(plan, self.running('orchestrator'), self.running('coder'))
        self.join('--orchestrator-pane', orchestrator, '--coder-pane', coder)
        self.wait_for(lambda s: s['orchestrator_ready'] and s['coder_ready'], what='both READY')
        self.say(coder, '/clear')
        self.wait_for(lambda s: s['coder_ready'] and len([p for p in self.log('coder', 'prompt') if '· setup ·' in p]) == 2,
                      what='a second brief and READY')
        self.say(orchestrator, 'Please create a.txt.')
        status = self.wait_for(lambda s: s.get('reason') == 'done', what='PAIR-DONE')
        self.assertEqual(status['relayed'], {'orchestrator': 1, 'coder': 1})
        self.assertIn('Step 1: create a.txt', self.log('coder', 'prompt')[2])

    def test_compacting_between_turns_brings_the_brief_again(self):
        # Found live: after /compact a joined Claude no longer knew the relay, and your next message came first.
        plan = {'orchestrator': ['(brief)', '(brief again)', 'Step 1: create a.txt', 'All verified.\nPAIR-DONE']}
        orchestrator, coder = self.window(plan, self.running('orchestrator'), self.running('coder'))
        self.join('--orchestrator-pane', orchestrator, '--coder-pane', coder)
        self.wait_for(lambda s: s['orchestrator_ready'] and s['coder_ready'], what='both READY')
        self.say(orchestrator, '/compact')
        self.wait_for(lambda s: s['orchestrator_ready'] and len([p for p in self.log('orchestrator', 'prompt')
                                                                 if '· setup ·' in p]) == 2, what='the brief again')
        self.give_task(orchestrator)
        status = self.wait_for(lambda s: s.get('reason') == 'done', what='PAIR-DONE')
        self.assertEqual(status['relayed'], {'orchestrator': 1, 'coder': 1})
        self.assertNotIn('READY', ' '.join(self.log('coder', 'prompt')[1:]))  # The second READY went nowhere.

    def test_compacting_mid_turn_adds_a_reminder_to_the_next_message(self):
        plan = {'orchestrator': ['(brief)', 'Step 1: create a.txt', 'Step 2: create b.txt', 'All verified.\nPAIR-DONE'],
                'compact': {'coder': [2]}}
        orchestrator, coder = self.window(plan, self.running('orchestrator'), self.running('coder'))
        self.join('--orchestrator-pane', orchestrator, '--coder-pane', coder)
        self.give_task(orchestrator)
        self.wait_for(lambda s: s.get('reason') == 'done', what='PAIR-DONE')
        prompts = self.log('coder', 'prompt')
        self.assertNotIn('Relay reminder', prompts[1])
        self.assertIn('Relay reminder: you are the coder', prompts[2])  # Step 2, after the compaction during step 1.
        self.assertEqual(len([p for p in prompts if '· setup ·' in p]), 1)

    def test_a_joined_agent_quitting_stops_the_relay(self):
        plan = {'orchestrator': ['(brief)', 'Step 1: create a.txt']}
        orchestrator, coder = self.window(plan, self.running('orchestrator'), self.running('coder', wrapped=True))
        self.join('--orchestrator-pane', orchestrator, '--coder-pane', coder)
        self.wait_for(lambda s: s['orchestrator_ready'] and s['coder_ready'], what='both READY')
        self.say(coder, '/quit')
        status = self.wait_for(lambda s: s.get('mode') == 'stopped', timeout=15, what='relay stop')
        self.assertEqual(status['reason'], 'coder exited')

    def test_a_joined_claude_error_is_tried_again(self):
        plan = {'orchestrator': ['(brief)', 'Step 1: create a.txt', 'All verified.\nPAIR-DONE'],
                'fail': {'coder': {'2': 'overloaded'}}}
        orchestrator, coder = self.window(plan, self.running('orchestrator'), self.running('coder'),
                                          AGENT_PAIR_RETRY_SCALE='0.02')
        self.join('--orchestrator-pane', orchestrator, '--coder-pane', coder)
        self.give_task(orchestrator)
        self.wait_for(lambda s: s.get('reason') == 'done', timeout=60, what='PAIR-DONE after the error')
        self.assertIn('stopped on an error (overloaded)', self.log('coder', 'prompt')[2])

    def test_a_joined_claude_at_work_without_a_spinner_gets_no_note(self):
        plan = {'orchestrator': ['(brief)', 'Step 1: create a.txt', 'All verified.\nPAIR-DONE'],
                'work': {'coder': {'2': {'seconds': 8, 'spinner': False}}}}
        orchestrator, coder = self.window(plan, self.running('orchestrator'), self.running('coder'),
                                          AGENT_PAIR_ENDED_SECONDS='3')
        self.join('--orchestrator-pane', orchestrator, '--coder-pane', coder)
        self.give_task(orchestrator)
        self.wait_for(lambda s: s.get('reason') == 'done', timeout=60, what='PAIR-DONE')
        self.assertFalse(any(p.startswith('[relay → coder]') for p in self.log('coder', 'prompt')))


class Units(unittest.TestCase):
    """The relay's bookkeeping driven directly, with tmux and the clock under the test's control."""

    def make(self, attached=(), agents=('codex', 'claude'), **env):
        for key, value in dict(env, AGENT_PAIR_CODEX_BIN='/fake/codex').items():
            os.environ[key] = value
            self.addCleanup(os.environ.pop, key, None)
        pair = load_pair_module()
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        roles = {role: {'agent': agent, 'label': agent.title(), 'attached': role in attached, 'token': 'T:' + role}
                 for role, agent in zip(('orchestrator', 'coder'), agents)}
        config = {'folder': str(directory), 'panes': {'orchestrator': '%0', 'coder': '%1'}, 'roles': roles}
        config['setup'] = {role: pair.setup_text(config, role) for role in attached}
        (directory / 'config.json').write_text(json.dumps(config))
        self.calls, self.busy, self.command, self.screen = [], False, 'claude', ''
        pair.tmux = self.tmux
        pair.screen_busy = lambda pane: self.busy
        pair.notify = lambda *args: None
        pair.print = lambda *args, **kwargs: None  # The relay strip's lines; relay.log keeps them.
        pair.time = type('Clock', (), {name: staticmethod(getattr(time, name))
                                       for name in ('monotonic', 'time', 'strftime', 'time_ns')})
        pair.time.sleep = staticmethod(lambda seconds: None)
        relay = pair.Relay(directory)
        relay.status.update(coder_ready=True, orchestrator_ready=True)
        relay.side = {'orchestrator': 'idle', 'coder': 'idle'}
        relay.queue = {'orchestrator': [], 'coder': []}
        relay.setup_sent = {'orchestrator': True, 'coder': True}
        return pair, relay, directory

    def tmux(self, *args, check=True, **kwargs):
        self.calls.append((args, kwargs.get('input')))
        out = self.command if '#{pane_current_command}' in args else self.screen if 'capture-pane' in args else ''
        return type('Done', (), {'stdout': out, 'stderr': '', 'returncode': 0})()

    def pasted(self):
        return [text for args, text in self.calls if args[0] == 'load-buffer']

    def enters(self):
        return [args for args, _ in self.calls if args[0] == 'send-keys' and args[-1] == 'Enter']

    def instruct(self, pair, relay, text='Build the row.'):
        relay.status.update(ball='coder', round=1)
        relay.queue['coder'].append({'kind': 'instruction', 'round': 1, 'text': text})
        relay.deliver()
        return relay.pending['coder']['text']

    # delivery is confirmed by the turn that carries it
    def test_only_the_turn_carrying_the_message_confirms_it(self):
        pair, relay, directory = self.make()
        sent = self.instruct(pair, relay)
        self.assertEqual(sent, '[orchestrator → coder]\n\nBuild the row.')
        relay.prompt('coder', 'are you there?')  # Your message got in first.
        self.assertIn('coder', relay.pending)
        relay.reply('coder', 'Yes, working on it.')
        self.assertIn('coder', relay.pending)  # Still waiting for the instruction's own turn.
        self.assertEqual(relay.queue['orchestrator'], [])
        relay.prompt('coder', f'<pasted_content id="1">\n{sent}\n</pasted_content id="1">')
        self.assertNotIn('coder', relay.pending)
        self.assertEqual(relay.origin['coder'], 'relay')
        relay.reply('coder', 'Built the row.')
        self.assertEqual([item['text'] for item in relay.queue['orchestrator']], ['Built the row.'])

    def test_words_typed_before_the_paste_still_confirm_it(self):
        pair, relay, directory = self.make()
        sent = self.instruct(pair, relay)
        relay.prompt('coder', 'half-typed words ' + sent.replace('\n\n', '\n'))
        self.assertNotIn('coder', relay.pending)
        self.assertEqual(relay.origin['coder'], 'relay')

    def test_a_header_an_agent_copies_into_its_reply_is_dropped(self):
        pair, relay, directory = self.make()
        sent = self.instruct(pair, relay)
        relay.prompt('coder', sent)
        relay.reply('coder', '**[coder → orchestrator]**\n\nBuilt the row; see [coder → orchestrator] notes.')
        self.assertEqual(relay.queue['orchestrator'][0]['text'], 'Built the row; see [coder → orchestrator] notes.')
        relay.deliver()
        self.assertEqual(relay.pending['orchestrator']['text'].count('[coder → orchestrator]'), 2)  # Ours, and the quoted one.
        self.assertTrue(relay.pending['orchestrator']['text'].startswith('[coder → orchestrator]\n\nBuilt the row'))

    def test_a_turn_whose_start_went_unseen_takes_the_delivery(self):
        pair, relay, directory = self.make()
        self.instruct(pair, relay)
        relay.reply('coder', 'Built the row.')
        self.assertNotIn('coder', relay.pending)
        self.assertEqual([item['text'] for item in relay.queue['orchestrator']], ['Built the row.'])

    def test_a_busy_agent_holds_the_retry_and_a_lost_message_is_sent_again_unchanged(self):
        pair, relay, directory = self.make()
        sent = self.instruct(pair, relay)
        item = relay.pending['coder']
        item['since'] -= 100
        self.busy = True
        relay.deliver()
        self.assertEqual(self.enters(), [(('send-keys', '-t', '%1', 'Enter'))])  # Only the paste's own Enter.
        self.busy = False
        relay.deliver()
        self.assertEqual(len(self.enters()), 1)  # Quiet for a moment only.
        item['busy'] -= 13
        relay.deliver()
        self.assertEqual(len(self.enters()), 2)  # One more Enter once it has been quiet a while.
        item['busy'] -= 40
        relay.deliver()
        self.assertEqual((relay.status['mode'], relay.status['reason']), ('paused', 'delivery failed'))
        self.assertEqual(relay.queue['coder'][0]['composed'], sent)
        relay.resume()
        relay.deliver()
        self.assertEqual(self.pasted(), [sent, sent])
        self.assertEqual(relay.status['relayed']['orchestrator'], 1)  # Counted once.

    def test_a_joined_codex_gets_messages_through_its_queue(self):
        pair, relay, directory = self.make(attached=('coder',), agents=('claude', 'codex'))
        runs = []
        pair.run = lambda args, **kwargs: runs.append(args) or type('Done', (), {'returncode': 0, 'stdout': 'Queued', 'stderr': ''})()
        relay.watching['coder'].thread = 'T1'
        sent = self.instruct(pair, relay)
        self.assertEqual(runs, [['/fake/codex', 'queue', '--thread', 'T1', '--message', sent]])
        self.assertEqual(self.pasted(), [])
        self.assertEqual(relay.pending['coder']['via'], 'queue')
        relay.pending['coder']['since'] -= 50
        relay.deliver()
        self.assertEqual(self.enters(), [])  # An Enter could submit whatever you are typing.
        self.assertIn('coder', relay.pending)
        relay.pending['coder']['since'] -= 50
        relay.deliver()
        self.assertEqual(relay.status['reason'], 'delivery failed')

    def test_a_refused_queue_falls_back_to_pasting(self):
        pair, relay, directory = self.make(attached=('coder',), agents=('claude', 'codex'))
        pair.run = lambda args, **kwargs: type('Done', (), {'returncode': 1, 'stdout': '', 'stderr': 'error: no such thread'})()
        relay.watching['coder'].thread = 'T1'
        sent = self.instruct(pair, relay)
        self.assertEqual(self.pasted(), [sent])
        self.assertEqual(relay.pending['coder']['via'], 'paste')
        self.assertIn('no such thread', (directory / 'relay.log').read_text())

    def test_never_queues_for_an_agent_that_quit(self):
        pair, relay, directory = self.make(attached=('coder',), agents=('claude', 'codex'))
        runs = []
        pair.run = lambda args, **kwargs: runs.append(args)
        relay.watching['coder'].thread = 'T1'
        self.command = 'bash'
        relay.status.update(ball='coder', round=1)
        relay.queue['coder'].append({'kind': 'instruction', 'round': 1, 'text': 'Build the row.'})
        relay.deliver()
        self.assertEqual((relay.status['mode'], relay.status['reason']), ('stopped', 'coder is not running'))
        self.assertEqual(runs, [])

    # failed turns
    def test_retry_schedule(self):
        pair = load_pair_module()
        self.assertEqual([pair.retry_wait('rate_limit', n) for n in range(1, 7)], [300, 900, 1800, 3600, 3600, 3600])
        self.assertEqual([pair.retry_wait('transient', n) for n in range(1, 5)], [60, 180, 600, None])
        self.assertIsNone(pair.retry_wait('permanent', 1))
        self.assertEqual([pair.failure_kind(e) for e in ('rate_limit', 'usageLimitExceeded', 'overloaded', 'serverOverloaded',
                                                         'max_output_tokens', 'billing_error', 'unauthorized', 'something new')],
                         ['rate_limit', 'rate_limit', 'transient', 'transient', 'transient', 'permanent', 'permanent', 'transient'])
        self.assertEqual(pair.error_name("You've hit your usage limit. Upgrade to Pro or try again at 3:05 AM."), 'rate_limit')
        self.assertEqual(pair.error_name('{"type":"error","status":400,"error":{"message":"not supported"}}'), 'invalid_request')
        self.assertEqual(pair.error_name('stream disconnected before completion'), 'unknown')

    def test_failures_are_tried_again_then_pause(self):
        pair, relay, directory = self.make(AGENT_PAIR_RETRY_SCALE='0')
        sent = self.instruct(pair, relay)
        relay.prompt('coder', sent)
        relay.failed('coder', 'overloaded', 'API Error: overloaded')
        self.assertEqual((relay.side['coder'], relay.status['failures']['coder']['count']), ('idle', 1))
        relay.retries()
        self.assertEqual(relay.queue['coder'][0]['text'], pair.RETRY_NOTE['coder'].format(error='overloaded'))
        relay.retries()
        self.assertEqual(len(relay.queue['coder']), 1)  # Once per failure.
        relay.queue['coder'].clear()
        relay.status['failures']['coder']['count'] = 3
        relay.origin['coder'] = 'relay'
        relay.failed('coder', 'overloaded')
        self.assertEqual((relay.status['mode'], relay.status['reason']), ('waiting', 'error'))
        relay.retries()
        self.assertEqual(relay.queue['coder'], [])  # Nothing while it waits for you.
        relay.resume()
        relay.retries()
        self.assertEqual(len(relay.queue['coder']), 1)
        self.assertEqual(relay.status['failures']['coder']['count'], 0)
        relay.prompt('coder', '[relay → coder]\n\n' + relay.queue['coder'][0]['text'])
        relay.reply('coder', 'Built the row.')
        self.assertEqual(relay.status['failures'], {})  # A good turn ends the streak.

    def test_a_sign_in_error_pauses_at_once(self):
        pair, relay, directory = self.make()
        relay.origin['coder'] = 'relay'
        relay.status['open_round'] = 1
        relay.failed('coder', 'authentication_failed')
        self.assertEqual(relay.status['reason'], 'error')
        self.assertIsNone(relay.status['failures']['coder']['due'])

    def test_your_turn_failing_is_not_retried(self):
        pair, relay, directory = self.make()
        relay.status.update(open_round=1, ball='coder')
        relay.prompt('coder', 'How is it going?')
        relay.failed('coder', 'rate_limit')
        self.assertEqual(relay.status['failures'], {})
        self.assertIn('coder', relay.after_you)  # It still owes its report: reminded later.
        relay.status['failures']['coder'] = {'count': 1, 'error': 'rate_limit', 'kind': 'rate_limit', 'due': 0}
        relay.prompt('coder', 'Hello again')
        self.assertEqual(relay.status['failures'], {})  # Your message takes over from a waiting retry.

    def test_codex_errors_on_screen(self):
        pair, relay, directory = self.make()
        self.screen = '› do it\n■ stream disconnected before completion\n› next\n• answer\n■ You\'ve hit your usage limit.\n› '
        self.assertEqual(pair.screen_error('%1'), "You've hit your usage limit.")
        self.screen = '■ You\'ve hit your usage limit.\n› go\n■ Conversation interrupted - tell the model what to do differently.\n› '
        self.assertEqual(pair.screen_error('%1'), '')  # Only the latest "■" line counts.
        self.screen = '• All done.\n› Ask Codex to do anything'
        self.assertEqual(pair.screen_error('%1'), '')

    # interrupts, stalls, sessions
    def test_codex_interrupts(self):
        pair, relay, directory = self.make()
        sent = self.instruct(pair, relay)
        relay.prompt('coder', sent)
        relay.interrupting['coder'] = time.monotonic()
        relay.interrupted('coder')
        self.assertEqual(relay.queue['coder'][0]['text'], pair.STUCK_NOTE['coder'].format(minutes='30 minutes'))
        relay.queue['coder'].clear()
        relay.stall_time.clear()
        relay.prompt('coder', '[relay → coder]\n\ncarry on')
        relay.interrupted('coder')  # Your Esc: the turn ended; a note comes only after a quiet spell.
        self.assertEqual((relay.side['coder'], relay.queue['coder']), ('working', []))

    def test_a_coder_owing_nothing_gets_no_stall_note(self):
        pair, relay, directory = self.make()
        relay.side['coder'] = 'working'
        relay.stalled('coder', 'ended')
        self.assertEqual((relay.side['coder'], relay.queue['coder']), ('idle', []))

    def test_a_joined_agent_compacted_between_turns_is_briefed_again(self):
        pair, relay, directory = self.make(attached=('coder',))
        relay.status.update(round=2, ball='coder', open_round=2)
        relay.session_start('coder', 'compact')
        self.assertEqual(relay.queue['coder'][0]['kind'], 'setup')
        self.assertTrue(relay.status['coder_ready'])  # Until the brief goes, a reply is still a reply.
        relay.deliver()
        brief = relay.pending['coder']['text']
        self.assertTrue(brief.startswith('[relay → coder · setup · T:coder]') and brief.endswith('Instructions come next.'))
        self.assertFalse(relay.status['coder_ready'])
        relay.prompt('coder', brief)
        relay.reply('coder', 'READY')
        self.assertEqual((relay.status['coder_ready'], relay.queue['orchestrator']), (True, []))  # Not a report.
        self.assertIn('has the pair brief again', (directory / 'relay.log').read_text())

    def test_a_joined_agent_compacted_mid_turn_is_reminded_once(self):
        pair, relay, directory = self.make(attached=('coder',))
        relay.side['coder'] = 'working'
        relay.session_start('coder', 'compact')
        relay.side['coder'] = 'idle'
        sent = self.instruct(pair, relay)
        self.assertTrue(sent.endswith(pair.REMIND['coder']))
        relay.prompt('coder', sent)
        relay.reply('coder', 'Built the row.')
        relay.status['ball'] = 'coder'
        relay.queue['coder'].append({'kind': 'instruction', 'round': 2, 'text': 'Next.'})
        relay.deliver()
        self.assertEqual(relay.pending['coder']['text'], '[orchestrator → coder]\n\nNext.')
        launched = self.make()[1]
        launched.session_start('coder', 'compact')
        self.assertEqual(launched.status['reminders'], {})  # Its brief is its system prompt: compacting keeps it.

    def test_clear_briefs_a_joined_agent_again_and_hands_its_step_back(self):
        pair, relay, directory = self.make(attached=('coder',))
        relay.status.update(open_round=3, ball='coder', round=3)
        relay.queue['coder'].append({'kind': 'nudge', 'text': 'old note'})
        relay.session_start('coder', 'clear')
        self.assertEqual((relay.setup_sent['coder'], relay.status['coder_ready'], relay.status['open_round']), (False, False, 0))
        self.assertEqual([item['kind'] for item in relay.queue['coder']], ['setup'])
        self.assertTrue(relay.queue['coder'][0]['text'].startswith('[relay → coder · setup · T:coder]'))
        self.assertEqual(relay.queue['orchestrator'], [{'kind': 'note', 'text': pair.CLEARED_NOTE}])
        relay.session_start('orchestrator', 'resume')  # A launched agent keeps its brief in its system prompt.
        self.assertEqual(relay.queue['orchestrator'], [{'kind': 'note', 'text': pair.CLEARED_NOTE}])
        self.assertTrue(relay.status['orchestrator_ready'])
        relay.session_start('coder', 'startup')
        self.assertEqual([item['kind'] for item in relay.queue['coder']], ['setup'])

    def test_resume_tells_an_orchestrator_left_waiting_to_carry_on(self):
        pair, relay, directory = self.make()
        relay.status.update(mode='waiting', reason='stuck', ball='orchestrator', round=2)
        relay.resume()
        self.assertEqual(relay.queue['orchestrator'], [{'kind': 'nudge', 'text': pair.RESUMED_NOTE}])
        for status in ({'round': 0}, {'round': 2, 'ball': 'coder'}):
            relay = self.make()[1]
            relay.status.update({'mode': 'paused', 'reason': 'by you', 'ball': 'orchestrator', **status})
            relay.resume()
            self.assertEqual(relay.queue['orchestrator'], [])

    def test_quitting_is_confirmed_by_the_pane(self):
        pair, relay, directory = self.make()
        running = ['claude']
        pair.running_agent = lambda info: running[0]
        pair.pane_info = lambda pane: {'pane_id': pane}
        relay.handle({'role': 'coder', 'hook_event_name': 'SessionEnd', 'reason': 'clear'})
        self.assertEqual(relay.leaving, {})
        relay.handle({'role': 'coder', 'hook_event_name': 'SessionEnd', 'reason': 'prompt_input_exit'})
        relay.checked = 0
        self.assertTrue(relay.panes_alive())  # Not gone yet.
        running[0] = ''
        relay.checked = 0
        self.assertFalse(relay.panes_alive())
        self.assertEqual((relay.status['mode'], relay.status['reason']), ('stopped', 'coder exited'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
