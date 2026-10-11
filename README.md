# Linux Terminal Agents Workbench

A pure black Ghostty terminal, a mouse-driven workspace for Claude Code and Codex, agent pairs that hand work to each other, a shared memory both agents read, and a few quiet GNOME touches. It targets Ubuntu 26.04 with GNOME 50 on Wayland and an amd64 processor.

```bash
git clone https://github.com/Ashwin-Baduni/linux-terminal-agents-workbench.git
cd linux-terminal-agents-workbench
./install.sh              # see what would change
./install.sh --install    # install it
```

Running without arguments shows the plan. `--install` installs what is missing, merges into your existing settings and keeps a backup of the configuration files it changes. `./install.sh --rollback last` restores those files and desktop settings after checking for later edits. Packages, installed agent CLIs and personal memory written afterwards stay. Open a new terminal, log out and back in once, and sign in to `claude` and `codex` yourself.

[docs/install.md](docs/install.md) explains the options, what happens to your existing files, the agent permission preset and what a rollback covers.

- [Using the workspace](docs/workspace.md): menus, agent pairs, Park and layout recovery.
- [Shared memory](docs/memory.md): one place for both agents, stable project names and reinstall recovery.
- [Fonts and colours](docs/appearance.md): the shipped appearance and its source files.
- [Maintaining the setup](docs/maintenance.md): reviewed imports, tests and publication checks.

The memory starts empty. This repository contains the setup machinery, with no personal notes, credentials or conversations.

## Tests

```bash
tests/run.py --quick     # install into a scratch home and test it
tests/run.py             # the same, plus the slower relay suite
```

The project's own code is MIT licensed. Bundled upstream material keeps its own licence, listed in `THIRD_PARTY_NOTICES.md`.
