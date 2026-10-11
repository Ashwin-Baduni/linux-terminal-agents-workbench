#!/usr/bin/env python3
"""A stand-in for the Claude or Codex TUI, so the relay can be tested without tokens.

It is launched with the same arguments agent-pair gives the real agents, finds the
hook command in them, reads keystrokes and bracketed pastes from its terminal, and on
Enter calls the hook exactly as the real agents do (UserPromptSubmit, then Stop).
Started with --settings it behaves like Claude Code; otherwise like Codex: Esc is reported
as Interrupt, and a failed turn calls no hook at all and shows a "■" line instead.

Behaviour comes from the FAKE_PLAN environment variable (JSON):
  orchestrator: list of replies, used in order (the last one repeats)
  coder:        reply template; {first_line} is the instruction's first line
  coder_script: reply templates used in order instead (the last one repeats)
  delay:        seconds to "think" before replying (default 0.3)
  ignore_first_enter: the first Enter is dropped, like a TUI that was still busy
  exit_after:   {role: n} exits after n replies
  no_files:     the coder changes no files
  no_stop:      {role: n} the nth reply never calls the Stop hook, like a turn cut short with Esc
  hang:         {role: [n, ...]} those replies freeze with a spinner until Esc, like a hung command
These count prompts received (1-based), failed ones included:
  fail:         {role: {n: error}} that prompt's turn stops on an API error (Claude Code's error name)
  work:         {role: {n: {"seconds": s, "spinner": bool}}} that turn calls tools for s seconds
                before replying, with a spinner that never changes, or with no spinner at all
  compact:      {role: [n, ...]} the conversation is compacted during that turn, as auto-compact does
Typed commands: /clear, /compact and /quit report the session events the real agents do.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import termios
import time
import tty
import uuid

# A pair-launched agent knows its role from agent-pair; one that joins later is told by the test (FAKE_ROLE).
ROLE = os.environ.get('AGENT_PAIR_ROLE') or os.environ.get('FAKE_ROLE', 'coder')
PLAN = json.loads(os.environ.get('FAKE_PLAN', '{}'))
CLAUDE = '--settings' in sys.argv
SESSION = str(uuid.uuid4())
RELAYED = re.compile(r'\[(orchestrator|coder|relay) → (orchestrator|coder)')
# What the real Codex shows for a failed turn, by Claude Code's error name.
CODEX_ERRORS = {'rate_limit': "You've hit your usage limit. Upgrade to Pro or try again at 3:05 AM.",
                'model_not_found': '{"type":"error","status":400,"error":{"type":"invalid_request_error","message":"The model is not supported."}}'}


def hook_command(args):
    if '--settings' in args:
        settings = json.loads(args[args.index('--settings') + 1])
        return settings['hooks']['Stop'][0]['hooks'][0]['command']
    for arg in args:
        match = re.match(r'hooks\.Stop=.*command=("(?:[^"\\]|\\.)*")', arg)
        if match:
            return json.loads(match.group(1))
    raise SystemExit('fake agent: no hook command in arguments')


HOOK = hook_command(sys.argv[1:])
LOG = Path(os.environ.get('FAKE_LOG') or os.environ.get('AGENT_PAIR_DIR', '.')) / f'fake-{ROLE}.jsonl'


def call_hook(event, **fields):
    payload = {'hook_event_name': event, 'session_id': SESSION, 'model': 'fake-' + ROLE, **fields}
    subprocess.run(HOOK, shell=True, input=json.dumps(payload), text=True, check=False)


def record(kind, text):
    with LOG.open('a') as file:
        file.write(json.dumps({'kind': kind, 'text': text}) + '\n')


def show(text):
    print('\r\n' + text.replace('\n', '\r\n') + '\r\n› ', end='', flush=True)


replies = work = received = 0


def planned(name):
    return PLAN.get(name, {}).get(ROLE, {}).get(str(received))


def reply_to(prompt):
    global replies, work, received
    received += 1
    record('prompt', prompt)
    call_hook('UserPromptSubmit', prompt=prompt)
    if replies + 1 in PLAN.get('hang', {}).get(ROLE, []):
        print('\r\n✻ Hanging… (1s · running a command)', end='', flush=True)
        while os.read(0, 1) != b'\x1b':
            pass  # Frozen until Esc, which ends the turn without a Stop hook, as both agents do.
        replies += 1
        record('reply', '(interrupted)')
        # The spinner line goes, as in the real agents; Codex says so with a "■" line and its Interrupt hook.
        print('\r\x1b[2K' + ('⎿ Interrupted' if CLAUDE else '■ Conversation interrupted - tell the model what to do '
                             'differently.') + '\r\n› ', end='', flush=True)
        if not CLAUDE:
            call_hook('Interrupt')
        return
    if received in PLAN.get('compact', {}).get(ROLE, []):
        call_hook('SessionStart', source='compact')
    busy = planned('work')
    if busy:
        if busy.get('spinner'):
            print('\r\n✻ Running tests… (2m 10s · esc to interrupt)', end='', flush=True)  # Never changes.
        deadline = time.monotonic() + busy['seconds']
        while time.monotonic() < deadline:
            call_hook('PreToolUse', tool_name='Bash')
            time.sleep(0.5)
            call_hook('PostToolUse', tool_name='Bash')
            time.sleep(0.5)
        print('\r\x1b[2K', end='', flush=True)
    time.sleep(PLAN.get('delay', 0.3))
    error = planned('fail')
    if error:
        record('reply', '(error: ' + error + ')')
        if CLAUDE:
            text = f'API Error: {error}'
            show('● ' + text)
            call_hook('StopFailure', error=error, last_assistant_message=text)
        else:
            show('■ ' + CODEX_ERRORS.get(error, 'stream disconnected before completion: error sending request'))
        return
    if 'READY' in prompt and re.match(r'\[relay → \w+ · setup', prompt.lstrip()):
        answer = 'READY'
    elif ROLE == 'orchestrator':
        script = PLAN.get('orchestrator', ['Step 1: create a.txt', 'Step 2: create b.txt', 'All verified.\nPAIR-DONE'])
        answer = script[min(replies, len(script) - 1)]
    else:
        first = next((line for line in prompt.split('\n\n', 1)[-1].splitlines() if line.strip()), '')
        if not PLAN.get('no_files'):
            Path(f'coder-step-{replies + 1}.txt').write_text(first + '\n')
        script = PLAN.get('coder_script') or [PLAN.get('coder', 'Done: {first_line}. Tests pass.')]
        answer = script[min(work, len(script) - 1)].format(first_line=first)
        work += 1
    replies += 1
    show('● ' + answer)
    record('reply', answer)
    if PLAN.get('no_stop', {}).get(ROLE) != replies:
        call_hook('Stop', last_assistant_message=answer)
    if PLAN.get('exit_after', {}).get(ROLE) == replies:
        raise SystemExit(0)


def command(name):
    """Slash commands start no turn: they report session events, as the real agents do."""
    global SESSION
    record('command', name)
    if name == '/clear':
        call_hook('SessionEnd', reason='clear')
        SESSION = str(uuid.uuid4())
        call_hook('SessionStart', source='clear')
        show('(conversation cleared)')
    elif name == '/compact':
        call_hook('SessionStart', source='compact')
        show('(conversation compacted)')
    elif name == '/quit':
        call_hook('SessionEnd', reason='prompt_input_exit' if CLAUDE else 'other')
        raise SystemExit(0)


def main():
    initial = sys.argv[-1] if RELAYED.match(sys.argv[-1]) else ''
    print(f'fake {ROLE} agent ready\r\n› ', end='', flush=True)
    call_hook('SessionStart', source='startup')
    if initial:
        reply_to(initial)
    old = termios.tcgetattr(0)
    tty.setraw(0)
    sys.stdout.write('\x1b[?2004h')  # Ask for bracketed paste, like the real TUIs.
    sys.stdout.flush()
    composer, pasting, buffer, enters = b'', False, b'', 0
    try:
        while True:
            buffer += os.read(0, 65536)
            while buffer:
                if buffer.startswith(b'\x1b[200~'):
                    pasting, buffer = True, buffer[6:]
                elif buffer.startswith(b'\x1b[201~'):
                    pasting, buffer = False, buffer[6:]
                elif buffer[:1] == b'\x1b' and len(buffer) < 6:
                    break  # Wait for the rest of a possible paste marker.
                elif buffer[:1] in (b'\r', b'\n') and pasting:
                    composer, buffer = composer + b'\n', buffer[1:]  # tmux pastes newlines as CR.
                elif buffer[:1] in (b'\r', b'\n'):
                    buffer = buffer[1:]
                    enters += 1
                    if PLAN.get('ignore_first_enter') and enters == 1:
                        continue
                    if composer.strip():
                        prompt, composer = composer.decode('utf-8', 'replace'), b''
                        if prompt.strip() in ('/clear', '/compact', '/quit'):
                            command(prompt.strip())
                        else:
                            reply_to(prompt)
                elif buffer[:1] == b'\x03':
                    return
                else:
                    composer, buffer = composer + buffer[:1], buffer[1:]
    finally:
        termios.tcsetattr(0, termios.TCSADRAIN, old)


if __name__ == '__main__':
    main()
