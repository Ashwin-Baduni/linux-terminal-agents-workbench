# Memory templates

These files start a new, empty shared memory. They hold rules and empty indexes only, never anyone's notes.

- `MEMORY.md` becomes `~/ai-memory/MEMORY.md` when that file does not exist yet. An existing memory is never replaced or rewritten by the installer.
- `CLAUDE.md` is the block the installer adds to `~/.claude/CLAUDE.md` when that file does not already import `~/ai-memory/MEMORY.md`. Anything else in the file stays.

A project's memory is created later by `ai-memory link` in that project's folder. It starts as an empty `MEMORY.md` index in `~/ai-memory/projects/<name>/`.

Runtime data stays out of this repository: the notes themselves, `~/ai-memory/.git`, the `.ai-memory` markers and `.claude/settings.local.json` files in project folders, and the index lock in `~/.local/state/ai-memory/`.
