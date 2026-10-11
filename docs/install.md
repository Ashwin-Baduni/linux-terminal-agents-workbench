# Installing

## What you need

The installer is tested on Ubuntu 26.04 with GNOME 50 in a Wayland session on an amd64 computer. Elsewhere it stops before changing anything and says why. Python 3.11 or newer must be there, which Ubuntu includes. Run it as your own user from a terminal inside your desktop session. It asks for your password through `sudo` only when it installs Ubuntu packages.

## Install

```bash
git clone https://github.com/Ashwin-Baduni/linux-terminal-agents-workbench.git
cd linux-terminal-agents-workbench
./install.sh              # read the plan; nothing changes
./install.sh --install    # apply it
```

When it finishes, open a new terminal window. Log out and back in once so GNOME loads the clipboard extension. Then run `claude` and `codex` once each to sign in. The installer never signs in for you.

## Options

| Option | What it does |
|---|---|
| `--check` | Shows the plan and changes nothing. This is also what happens with no option. |
| `--install` | Installs the selected components. |
| `--only terminal,memory` | Chooses components. Whatever they need is added: workspace needs terminal, and agents needs workspace. |
| `--agent-policy full-access` | Also turns off the approval prompts of Claude Code and Codex (see below). |
| `--claude-version`, `--codex-version` | The release used when that CLI is missing. The default is `latest`. Claude Code also takes `stable`, and both take an exact version such as `2.1.89`. |
| `--no-activate` | Files only. No packages, agent CLIs, GNOME settings or services. |
| `--target-home DIR` | Stages the files into a separate folder instead of your home, for trying it out. Nothing on the computer itself changes. Your own home, or a folder that contains it, is refused here. |
| `--rollback BACKUP` | Restores the state before an installation. `last` means the most recent one. |
| `--keep-changed` | With `--rollback`: keeps files, settings and services changed since the installation, and restores everything else. |

## Components

| Component | What you get |
|---|---|
| terminal | Ghostty with the pure black theme and JetBrains Mono, the Bash setup, Starship, Atuin, zoxide, fzf, bat, eza, glow, micro, Yazi and their colours, plus the Markdown viewer and Ctrl+Alt+T in Ghostty |
| workspace | The mouse-driven tmux workspace, the Directory sidebar, the menu-enabled agent tabs, Open Workspace Here in Files, the dock launcher and the two services that watch agents and save layouts |
| agents | Agent pairs (the relay), Claude Code and Codex when they are missing, and their theme, voice, copy and hook settings |
| memory | An empty shared memory in `~/ai-memory`, the `ai-memory` helper and its snapshot timer, and the links both agents read |
| clipboard | The Super+V clipboard list. It keeps text only and forgets it at logout. |
| gnome-look | Dark Yaru blue style and a bottom dock that hides itself |

`manifests/files.toml` lists every file and where it goes. `manifests/components.toml` lists the packages, services and GNOME settings.

## What happens to your existing files

Nothing is overwritten without a backup. The plan names every file before anything changes.

- **Plain configuration files** (themes, scripts, units) are replaced when they differ. Their permissions are kept, and scripts stay executable.
- **`~/.bashrc`** gets one managed block. An older version of the same terminal block is replaced, once. Everything else stays.
- **`~/.inputrc`** gets the key bindings added once.
- **`~/.claude/settings.json`** gets the theme, voice, copy and hook settings. Your other settings and hooks stay. The workspace hook is added unless a group with the same event and matcher already runs the same command. A hook limited to some tools does not count. A new file is private (0600).
- **`~/.codex/config.toml`** gets the copy settings, changed in place. Projects, providers, MCP servers and comments stay. A new file is private.
- **`mimeapps.list`** gets defaults only for file types that have none. Your own choices win.
- **The git ignore list** (the one git uses, `core.excludesfile` included) gets the two memory patterns.
- **`~/.claude/CLAUDE.md`** gets the shared-memory import unless it is already there.
- **`~/.codex/AGENTS.md`** becomes a link to the shared memory when you have none. If you already have one, it keeps your instructions and gains a short block that tells Codex to read the shared memory. `AGENTS.override.md` gets the same block, because Codex reads it first.
- **`~/ai-memory`**: an existing memory is never touched. A new one starts with the rules and empty indexes.
- **Symlinks:** a symlink where a file goes is replaced by the file, and the file it pointed to is left alone. A symlinked folder is followed only when it stays inside your home. The installer refuses one that leads elsewhere. The links it creates are worked out from where their folder really is, so they still work inside a symlinked `~/.codex` or `~/.config/yazi-sidebar`.
- **Masked services:** these keep their mask:
  - a unit you masked, whose file in `~/.config/systemd/user` links to `/dev/null`;
  - a unit masked only until logout (`systemctl --user mask --runtime`);
  - a unit masked for every user in `/etc/systemd/user` or `/run/systemd/user`;
  - any unit systemd reports as masked.

  On a real installation the installer asks systemd before planning any file. For a masked unit it writes no unit file, so the mask can't be undone, and it does not enable or start the unit. It names the unit at the end.

`CODEX_HOME` and `CLAUDE_CONFIG_DIR` are honoured when they point inside your home. If you use either one, set it for the whole desktop session, not only in `~/.bashrc`. The workspace, its menus and its monitor service start outside your shell, and they would otherwise read the default folders. Put the variable in a file such as `~/.config/environment.d/agents.conf`, for example `CODEX_HOME=$HOME/.config/codex`, then log out and back in. The installer only points out a mismatch. It never changes the session environment itself.

## Claude Code and Codex

An agent CLI you already have is kept and never upgraded. A missing one is installed with its vendor's official native installer:

- **Claude Code:** [code.claude.com/docs/en/setup](https://code.claude.com/docs/en/setup), `https://claude.ai/install.sh`
- **Codex:** [learn.chatgpt.com/docs/codex/cli](https://learn.chatgpt.com/docs/codex/cli), `https://chatgpt.com/codex/install.sh`

The installer downloads each script over HTTPS into `~/.local/state/workbench/downloads/` and prints its SHA-256. Before running a script, it checks it against the interface that was reviewed: the argument that selects the release, the non-interactive switch, the `~/.local/bin` destination, and the list of releases the script accepts. A script that has changed is not run. The installer stops and points to the vendor's own instructions. A script that passes is run as you and kept with a `SHA256SUMS` file for inspection. `./install.sh --check` shows which installers would run, with which release. Both CLIs land in `~/.local/bin`. The versions found or installed are printed at the end and recorded in `~/.local/state/workbench/installed.json`. Claude Code's native install keeps itself updated, as its documentation describes.

## Agent permissions

By default the installer leaves your approval and sandbox settings and your model choices exactly as they are. `--agent-policy full-access` is a deliberate choice: Claude Code starts in bypass-permissions mode, and Codex runs with `approval_policy = "never"`, `sandbox_mode = "danger-full-access"` and live web search. Both stop asking before they act. Use it only on a machine where that is what you want.

## GNOME and services

On a real installation the installer applies the GNOME settings in `payload/*/gsettings.json`:
- the default terminal and Super+T
- the dock launcher
- the clipboard extension, with Super+V taken from GNOME's notification list (Super+M still opens that list)
- with gnome-look, the dark Yaru blue style and the dock layout

Lists such as enabled extensions are added to, never replaced. Another installed extension that uses the same clipboard settings schema is switched off, not deleted. The extension's settings schema is compiled during installation.

It enables and starts the selected user services: `workspace-monitor.service`, `workspace-save.timer` and `ai-memory-snapshot.timer`. It refreshes bat's theme cache and the desktop database.

## Rollback

Every installation that changes something writes a journal and a copy of each file it replaces to `~/.local/state/workbench/backups/<time>/`. This happens before the first change. The journal also records the previous GNOME values and the services' previous state.

`./install.sh --rollback last` (or the folder printed at the end) first checks everything. If a file, a GNOME setting or a service's enablement changed after the installation, it lists what changed and restores nothing. Run it again with `--keep-changed` to keep those changes and restore everything else. A rollback also refuses a backup copy that no longer matches its checksum, and a folder that has since become a symlink leading outside your home.

When nothing conflicts, the rollback goes in this order:
1. It stops and disables the services the installation started or enabled, while their unit files are still in place.
2. It brings back the old files, symlinks and permissions, and removes what the installation created.
3. It sets the GNOME values back.
4. It reloads systemd and restarts the services that were running before, so they run the restored files.

These stay after a rollback:
- installed Ubuntu packages and snaps
- the agent CLIs
- sign-ins
- memory you wrote after installing (a new `~/ai-memory/MEMORY.md` you have added to is kept)

If an installation fails, it undoes what it had done at once. That includes a step that failed halfway, such as a setting that was written before an error, or a service that was enabled but would not start. If something you changed during the installation is in the way, the undo keeps your change. It records the installation as failed, not rolled back, and names what it could not undo. The next run then refuses to continue until you have run `--rollback` (with `--keep-changed` if you want to keep your change).

If an installation is killed partway, the next run also refuses to continue until you run `--rollback` on it. That rollback covers the step the installation was on, too.

A rollback writes its own progress into the journal as it goes. If it stops partway, because a service fails to restart or the rollback is interrupted, the journal says so and lists what was already restored. The installer then refuses to install again until you run the same `--rollback` once more. That run continues from where the last one stopped. Before changing anything, each attempt records which services it intends to restart. A retry starts those if they ended up stopped, and leaves alone any service you stopped yourself. Services already stopped or disabled are listed among the restored items, so a partial report shows them too.

## Trying it without touching your setup

```bash
mkdir /tmp/try-home
./install.sh --install --target-home /tmp/try-home
./install.sh --rollback last --target-home /tmp/try-home
```

This writes and merges files inside `/tmp/try-home` only. It installs no packages or CLIs, changes no GNOME settings and starts no services. Paths in the installed files point at the target folder.

## Limits

- Only the tested Ubuntu, GNOME and Wayland versions are accepted for a real installation.
- The home path must not contain spaces or shell characters, because it is written into desktop entries and configuration files.
- Planning does not need Git: without it, the installer reads `core.excludesFile` straight from your Git configuration files. Git `include` directives are not followed in that case. Once Git has been installed, the plan is made again with Git itself.
- Before installing any package or agent CLI, the installer works out every file change. A broken settings file or an unsafe symlink therefore stops it before anything else happens.
- Fresh-desktop behaviour has to be rehearsed on a disposable Ubuntu 26.04 machine: the GNOME session, glyphs, the clipboard extension after login, and the real agent installers and sign-ins. The automated tests use stand-ins for the desktop, services and vendor installers.
- Hide Top Bar, display tuning and other personal machine settings are not included.
