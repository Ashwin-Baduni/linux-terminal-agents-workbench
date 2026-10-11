# Using the workspace

Open **Workspace** from the applications menu or dock. In Files, right-click a project folder and choose **Scripts → Open Workspace Here**. The command `work /path/to/project` opens the same workspace.

Each workspace starts with two terminal panes. Click a pane to focus it and drag a border to resize it. Right-click for **Agents…**, **Pair two agents…**, **Project / review…** and **Layout / output / resources…**. **Show Directory** adds the file browser when you need it.

Sign in to Claude Code and Codex before starting a pair. Pairing connects an orchestrator and a coder. The orchestrator's final replies become the coder's instructions, and the coder's replies return to the orchestrator. The relay keeps one message in flight. Use **Pair controls…** to manage a running pair. The relay ends when the orchestrator finishes a reply with a line that says only `PAIR-DONE`; the coder cannot end it. Unpair before starting a different Codex conversation with `/new` or `/resume`.

**Copy everything** copies the available conversation or terminal history. The right-click menu also provides paste, pane splitting and opening file paths. Ordinary agent tabs get the mouse menu too when started through the installed shell wrappers.

## Closing and restoring

Closing the workspace window stops its agents. Choose **Park workspace** to hide the window while keeping its processes running. Reopen it from the Workspace launcher.

Layouts are saved every ten minutes. **Save workspace layouts** saves immediately, and the launcher offers **Restore saved layouts** after a restart. Restoration recovers layout, folders and labels. It does not restart old agent processes or recover an agent account's conversations; resume those through the agent itself.

## Checking the setup

```bash
systemctl --user status workspace-monitor.service workspace-save.timer
ai-memory status
```

The monitor updates agent indicators. Blue means working, green means finished, red means input is needed or an error occurred, and grey means ready or stopped. The active pane has a light border.

**Super+V** opens the clipboard list after the extension has loaded at login. It stores text in memory and forgets it at logout. **Super+M** still opens notifications.
