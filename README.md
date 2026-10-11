# Linux Terminal Agents Workbench

A pure black Ghostty terminal, a mouse-driven workspace for Claude Code and Codex, agent pairs that hand work to each other, a shared memory both agents read, and a few small GNOME touches. It is built for Ubuntu 26.04 with GNOME on Wayland.

The installer is still being built. For now the repository holds the source files, the manifest that says where each one goes, and the tests.

```bash
tests/run.py --quick     # stage everything into a scratch home and test it
tests/run.py             # the same, plus the slower relay suite
```

The project's own code is MIT licensed. Bundled upstream material keeps its own licence, listed in `THIRD_PARTY_NOTICES.md`.
