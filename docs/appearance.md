# Appearance

The repository contains the actual configuration files. They remain readable so you can change a preference without regenerating an archive.

| Setting | Value | Source |
|---|---|---|
| Terminal font | JetBrains Mono, 10 pt | `payload/terminal/ghostty/config` |
| Line spacing | Cell height increased by 15% | Ghostty config |
| Padding | 12 px horizontal, 8 px vertical | Ghostty config |
| Background / text | `#000000` / `#ffffff` | `payload/terminal/ghostty/themes/amoled-black` |
| Selection | `#3f3f3f` with white text | Ghostty theme |
| Accent palette | Catppuccin Mocha on pure black | Ghostty theme |
| Working / finished | `#89b4fa` / `#a6e3a1` | `payload/workspace/tmux/workspace.conf` |
| Needs input / error | `#ff5555` | Workspace tmux config |
| Ready / stopped | `#9698a8` / `#6c7086` | Workspace tmux config |
| Active border | `#e4e4e4` | Workspace tmux config |
| Orchestrator / coder | `#cba6f7` / `#ffa891` | Workspace tmux config |
| GNOME style | Dark Yaru blue, blue accent | `payload/gnome-look/gsettings.json` |
| Dock | Bottom, 64 px icons, hides when appropriate | GNOME look settings |

Matching themes are supplied for Claude Code, bat, glow, micro and Yazi. Starship and Atuin settings are under `payload/terminal/`. Fonts are installed through Ubuntu packages; font binaries are not copied from a personal home directory.

The GNOME component contains a small list of desktop preferences. Monitor arrangement, scaling, HDR, GPU tuning, keyboard hardware, remote access, browser profiles and personal applications are outside this installer. System UI fonts remain those of the Ubuntu desktop.
