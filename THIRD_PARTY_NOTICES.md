# Third-party notices

The project's own code and configuration are under the MIT licence in `LICENSE`. The material below comes from other projects and keeps its own licence. `manifests/third-party.toml` lists the same material for the tests.

| Material | Where it is here | Upstream | Licence | Changes |
|---|---|---|---|---|
| tmux-resurrect, commit `cff343cf9e81` | `vendor/tmux-resurrect/` | [tmux-plugins/tmux-resurrect](https://github.com/tmux-plugins/tmux-resurrect) | MIT, `vendor/tmux-resurrect/LICENSE.md` | None. Only the runtime scripts are included. |
| bash-preexec 0.7.0 | `vendor/bash-preexec/` | [rcaloras/bash-preexec](https://github.com/rcaloras/bash-preexec) | MIT, `licenses/bash-preexec.LICENSE` | None. The author's header is kept. |
| Yazi git plugin, revision `33dde28` | `vendor/yazi-plugins/git.yazi/` | [yazi-rs/plugins](https://github.com/yazi-rs/plugins) | MIT, `LICENSE` beside it and `licenses/yazi-plugins.LICENSE` | None. |
| Catppuccin Mocha TextMate theme (bat and Yazi previews) and glamour style (glow) | `payload/terminal/themes/`, `payload/terminal/glow/catppuccin-amoled.json` | [Catppuccin](https://github.com/catppuccin) | MIT, `licenses/catppuccin.LICENSE` | Backgrounds changed to pure black and near-black greys. |
| Catppuccin Mocha colours | the AMOLED Ghostty theme and the Workspace pane colours | [Catppuccin](https://github.com/catppuccin) | MIT, `licenses/catppuccin.LICENSE` | Colour values only. The themes are original. |
| Starship Nerd Font Symbols preset | `payload/terminal/starship.toml` | [starship/starship](https://github.com/starship/starship) | ISC, `licenses/starship.LICENSE` | None. It is the output of `starship preset nerd-font-symbols`. |

The installer copies these licence files to `~/.local/share/workspace/licenses/`, next to the installed code.

## Installed from their own sources, not included here

Ghostty, tmux, Yazi, the fonts, the shell tools, Claude Code and Codex are installed from their usual packages or installers under their own licences. No binaries, fonts or caches are copied into this repository.

The Hide Top Bar GNOME extension (GPL-3.0-or-later) is not part of this repository. Anyone who wants it can install it from GNOME Extensions.
