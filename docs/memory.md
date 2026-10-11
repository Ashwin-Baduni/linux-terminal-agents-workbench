# Shared memory

The installer creates the memory rules, helper and empty indexes. It never brings another person's notes, conversations, project registrations or credentials into your account.

```text
~/ai-memory/
  MEMORY.md                   global rules and index
  projects/
    example-project/
      MEMORY.md               this project's index, created by ai-memory link
```

Global facts belong at the top level. Project facts belong in `projects/<name>/`. Both agents use the same files. Claude's global instructions import the shared index; Codex's global instructions link to it or gain a reference when instructions already exist. An existing Codex override receives the reference too.

## Connect a project

From the project's top folder:

```bash
ai-memory link example-project
ai-memory where
```

The helper writes a one-line `.ai-memory` marker naming the project memory and configures Claude's local `autoMemoryDirectory`. These machine-specific files are ignored by Git. Codex follows the shared rules: find the marker in the current folder or its ancestors, read that project's index, and read the relevant notes before relying on them.

The name in `.ai-memory` is stable. Renaming or moving the project folder leaves its notes in the same place. Run `ai-memory link` in the moved folder to refresh its registered location, then use `ai-memory status` to check the links. A second copy of a project should have its own memory name unless you are deliberately reconnecting the moved original.

You can tell a new agent to look for a project under `~/ai-memory/projects/`. It need not recover the old working-directory path to find the notes.

## Reinstalling Linux

Back up `~/ai-memory` privately, including its local `.git` directory if you want its history. Restore it into your new home. Reinstall this workbench, then run `ai-memory link example-project` in each restored or recloned project folder. A fresh clone does not contain the ignored marker or local Claude settings.

The installer preserves an existing memory index and notes. It installs the machinery around them. Notes containing machine-specific paths may still need updating when the user name or directory layout changes.

## Local history

The snapshot timer commits changes every fifteen minutes. To save now:

```bash
ai-memory snapshot
git -C ~/ai-memory log --oneline
systemctl --user status ai-memory-snapshot.timer
```

The helper never pushes this history. Local history can recover an overwritten note; a separate private backup is needed to survive a lost disk or reinstall. Never add the notes, this history or credentials to the public workbench repository.
