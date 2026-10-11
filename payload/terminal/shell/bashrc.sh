case ":$PATH:" in
	*":$HOME/.local/bin:"*) ;;
	*) export PATH="$HOME/.local/bin:$PATH" ;;
esac

# --- Terminal tools ------------------------------------------------------------
# fzf: Ctrl+R fuzzy history, Ctrl+T fuzzy file picker with preview, Alt+C fuzzy cd
export FZF_DEFAULT_OPTS='--height 40% --layout reverse --border'
export FZF_CTRL_T_COMMAND='fdfind --type f --hidden --exclude .git'
export FZF_CTRL_T_OPTS="--preview 'batcat --color=always --style=numbers --line-range=:200 {}'"
export FZF_ALT_C_COMMAND='fdfind --type d --hidden --exclude .git'
eval "$(fzf --bash)"

# bat: syntax-highlighted file viewer that follows the terminal's colours
alias bat='batcat'
export BAT_THEME='Catppuccin Mocha'

# eza: listings with icons and git status. Plain ls is left alone so scripts
# and AI agents still get normal ls output.
alias ll='eza -l --icons --git --group-directories-first'
alias la='eza -la --icons --git --group-directories-first'
alias lt='eza --tree --level=2 --icons --git-ignore'

# Default text editor (used by yazi, git, Claude Code's Ctrl+G, etc.)
export EDITOR=micro VISUAL=micro

# y: open the yazi file browser; when you quit with q, the shell moves to the
# folder you were in.
y() {
	local tmp cwd
	tmp="$(mktemp -t "yazi-cwd.XXXXXX")"
	yazi "$@" --cwd-file="$tmp"
	IFS= read -r -d '' cwd < "$tmp"
	[ -n "$cwd" ] && [ "$cwd" != "$PWD" ] && builtin cd -- "$cwd"
	rm -f -- "$tmp"
}

# files: file browser for a split. Clicking "Close" (or pressing q) also
# closes the split it's in.
files() {
	yazi "$@"
	exit
}

# Shared Bash hooks let Starship, Atuin and workspace notifications cooperate.
# Give direct agent tabs the same copy/menu behavior as workspace panes.
# Noninteractive commands and existing tmux panes use the real binary directly.
if [[ -x "$HOME/.local/bin/agent-terminal" ]]; then
	codex() { "$HOME/.local/bin/agent-terminal" codex "$@"; }
	claude() { "$HOME/.local/bin/agent-terminal" claude "$@"; }
fi
source "$HOME/.local/share/workspace/bash-preexec.sh"
# Prompt, then zoxide (z <part of folder name>).
eval "$(starship init bash)"
eval "$(zoxide init bash)"

# Workspace history and long-command notifications. No cloud history sync.
if command -v atuin >/dev/null 2>&1; then
	eval "$(atuin init bash --disable-up-arrow)"
fi
__workspace_prompt() {
	local workspace_exit_code=$?
	if [[ -n "$TMUX_PANE" && -n "$TMUX" ]]; then
		"$HOME/.local/bin/workspace-control" shell-done "$TMUX_PANE" "$workspace_exit_code" >/dev/null 2>&1
	fi
	return "$workspace_exit_code"
}
if [[ -n "$TMUX_PANE" && -n "$TMUX" && -x "$HOME/.local/bin/workspace-control" ]]; then
	__workspace_preexec() {
		"$HOME/.local/bin/workspace-control" shell-start "$TMUX_PANE" >/dev/null 2>&1
		return 0
	}
	preexec_functions+=(__workspace_preexec)
	precmd_functions+=(__workspace_prompt)
fi
