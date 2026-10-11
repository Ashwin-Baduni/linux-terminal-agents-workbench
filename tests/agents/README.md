# agent-pair tests

`tests/run.py relay` (or `python3 tests/agents/test_offline.py`) runs the staged relay, hooks and tmux against fake agents (`fake_agent.py`), so it costs no tokens. It takes a few minutes and uses private tmux servers, so open Workspaces are not touched.

What it covers:

- **Basics:** the full loop to `PAIR-DONE`, nothing forwarded after `PAIR-DONE`, pause and resume, long messages sent as a file, a lost Enter, an agent exiting, same-vendor pairs.
- **Staying in sync:** one message in flight. The orchestrator's reply to you while the coder works rides back with the coder's report. The coder's answers to you stay private. An interrupted turn can be skipped. An empty report still hands the turn back.
- **Stalls:** a frozen turn is stopped with Esc and sent back to work, a turn that ended without a report gets a note, and stalling twice on one step goes to the orchestrator.
- **Gentle checks:** the quiet reminder every N reports. A loop, and rounds without file changes, get a note first and a pause only if they recur after it.
- **Pairing running agents:** both say READY and nothing moves until you give the orchestrator the task. Earlier chat stays private. A busy agent finishes first. A new partner opens in an idle shell. The relay never types into a shell. Unpairing keeps both agents. The same agents can be paired again after `PAIR-DONE`. The relay sits along the bottom and can move left.
- **Own messages:** Claude's background-task notices continue the coder's task instead of counting as the human; the human's message mid-task stays private and the coder is reminded to report.
- **Codex turns:** turns read from Codex's shared server become relay events: an interrupted turn is an Interrupt, a failed one a StopFailure with Codex's error name, a compaction is reported, and new items are a heartbeat.
- **Deliveries:** only the turn that carries a message's first words confirms it (also inside Claude's `<pasted_content>` tags, or after half-typed words); your turns leave it waiting; a busy screen holds the extra Enter; a message not taken pauses the pair and goes again unchanged on Resume, counted once. A joined Codex gets messages through `codex queue` (never an Enter), falls back to pasting when the queue refuses, and is never sent anything once its pane is back at a shell. A header an agent copies into its reply is dropped.
- **API errors:** Claude's StopFailure and a Codex "■" error line on a quiet screen are retried on a schedule (usage limits hourly in the end, others three times, sign-in errors pause at once); Resume retries; your message takes over from a waiting retry; your own turn failing is not retried.
- **Interrupts and heartbeat:** Codex's Interrupt after the relay's Esc goes straight to the note; after yours the note waits for a quiet spell. Tool calls keep a still screen, or one without a spinner, from counting as a stall, for launched and joined agents. A coder owing no report gets no stall note.
- **Sessions:** `/clear` hands the coder's step back to the orchestrator and briefs a joined agent again; `/compact` between turns brings the brief again, mid-turn a reminder with the next message; a joined agent quitting stops the relay once its pane is a shell; Resume tells an orchestrator left waiting to carry on.

`fake_agent.py` acts as Claude Code when started with `--settings` and as Codex otherwise (Esc reported as Interrupt, a failed turn shown as a "■" line with no hook); its plan options are listed at the top of the file.

These offline tests do not sign in to or call the real agents. Compatibility with a particular Claude Code or Codex release (hooks, session files, `codex queue`, screen recognition) is checked separately with real, signed-in agents before a release.
