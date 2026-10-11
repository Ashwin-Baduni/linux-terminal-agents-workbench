# Handoff — 2026-10-11

The workbench now has a sanitized source tree, a working installer and rollback, documentation and a completed Ubuntu 26.04 / GNOME 50 Wayland rehearsal. Work is committed locally on `feature/sanitized-source-tree`. Nothing has been pushed or merged.

## What is included

- Ghostty with JetBrains Mono at 10 pt, a pure black background and the matching shell/tool themes.
- The tmux workspace, Directory sidebar, mouse menus, layout recovery and agent relay.
- An empty shared-memory system for Claude Code and Codex, stable project markers and local snapshots. Folder renames retain the project memory name.
- The clipboard extension and the selected GNOME theme and dock settings.
- Component manifests, upstream licences, configuration merges, backups, conflict-aware rollback and publication checks.

Personal notes, credentials, conversations, histories, machine tuning and account state are excluded. Existing memory is preserved. Agent permission and model choices stay as they are unless `--agent-policy full-access` is explicitly selected. Public installation reconstructs the machinery; restore your own memory from a separate private backup.

## Run or test it

From this checkout, preview without making changes:

```bash
./install.sh --check
```

On a supported Ubuntu desktop, apply it:

```bash
./install.sh --install
```

Open a new terminal and log out and back in once. Run `claude` and `codex` to sign in. Open Workspace from the applications menu or dock. In each project, run `ai-memory link` to register or reconnect its memory.

To try the file installation without altering your own setup:

```bash
trial_home=$(mktemp -d)
./install.sh --install --target-home "$trial_home"
./install.sh --rollback last --target-home "$trial_home"
```

The scratch folder is left for inspection. A real rollback uses `./install.sh --rollback last`; it checks for later edits before changing anything. Packages, agent CLIs and memory written afterwards stay.

## Verification and limits

The repository, publication, installer, memory and copy suites passed, along with all nine workspace scenarios and 73 offline relay tests. The real VM passed installation, repeat installation, desktop menus, clipboard, memory rename/relink and snapshots, Park/restore, rollback and reinstall. See [the verification record](docs/verification.md) for counts, versions and scope.

Sign-ins and a pair of authenticated agents still need your accounts. Hardware-specific display and voice behavior were not tested in the VM. Only Ubuntu 26.04, GNOME 50, Wayland and amd64 are currently accepted for a real install.

[Installation](docs/install.md) explains merges and recovery. [Appearance](docs/appearance.md), [workspace use](docs/workspace.md), [shared memory](docs/memory.md) and [maintenance](docs/maintenance.md) document the setup and how to keep it public-safe.
