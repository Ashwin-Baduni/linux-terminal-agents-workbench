# Linux Terminal Agents Workbench

A pure black Ghostty terminal, a mouse-driven workspace for Claude Code and Codex, agent pairs that hand work to each other, a shared memory both agents read, and a few quiet GNOME touches. It is built for Ubuntu 26.04 with GNOME on Wayland.

```bash
git clone https://github.com/Ashwin-Baduni/linux-terminal-agents-workbench.git
cd linux-terminal-agents-workbench
./install.sh              # see what would change
./install.sh --install    # install it
```

The first command only shows the plan. The second installs what is missing, merges into your existing settings and keeps a backup of every file it touches. `./install.sh --rollback last` puts those files and settings back. Afterwards, open a new terminal, log out and back in once, and sign in to `claude` and `codex` yourself.

[docs/install.md](docs/install.md) explains the options, what happens to your existing files, the agent permission preset and what a rollback covers.

## Tests

```bash
tests/run.py --quick     # install into a scratch home and test it
tests/run.py             # the same, plus the slower relay suite
```

The project's own code is MIT licensed. Bundled upstream material keeps its own licence, listed in `THIRD_PARTY_NOTICES.md`.
