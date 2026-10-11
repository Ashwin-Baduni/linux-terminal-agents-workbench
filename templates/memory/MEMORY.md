# Global memory (shared by Claude Code and Codex)

`~/ai-memory/` is the memory Claude Code and Codex CLI share. This file loads into every session wherever it starts: Claude imports it from `~/.claude/CLAUDE.md`, and `~/.codex/AGENTS.md` is a symlink to it.

**Two scopes:**
- **Global (files at the top level here):** facts about the user, their preferences and this machine: anything that would still matter if the session had started in a different folder. "Update your global memory" means here.
- **Project (`projects/<name>/`):** facts about one folder or repo, shared by both agents. Such a folder holds a one-line `.ai-memory` file naming its project memory; look for it in the working folder and the folders above it (`ai-memory where` prints it). Renaming or moving the folder keeps the memory.
  - Claude: the folder's `.claude/settings.local.json` sets `autoMemoryDirectory` to it, so it is your auto-memory. If your auto-memory is somewhere else (a session started in a subfolder), use the project memory instead.
  - Codex: in such a folder, read its `MEMORY.md` at the start of the session, then the notes relevant to the task, and save project facts there.
  - A folder with no `.ai-memory` yet: before saving the first fact about it, run `ai-memory link` in its top folder (never in `~`), then save into the folder it prints.
  - Keep private notes out of a repo's own AGENTS.md and CLAUDE.md: those are committed, may be public, and hold only instructions for anyone working on the repo.

**How to save a memory:**
- One fact per file, kebab-case filename, frontmatter with `name`, `description`, `metadata.type` (user | feedback | project | reference). For feedback/project, follow with **Why:** and **How to apply:** lines. Link related files with `[[name]]`.
- Add one line to that scope's index: `- [Title](file.md) — hook`. The Index below is the global one; a project's is its own `MEMORY.md`.
- Update an existing file rather than adding a duplicate; delete memories that turn out wrong. Write absolute dates. Never store passwords, tokens, or keys.
- Both Claude and Codex write here — re-read a file right before editing it.
- Read the linked file before relying on an index line; verify a remembered path or command still exists before recommending it.
- `~/ai-memory` is a local git repository with no remote; a timer commits changes every 15 minutes, so a lost or broken note can be restored from its history.

## Index

## Projects
Private memory for one folder each, in `projects/<name>/`. `ai-memory status` checks the links.
