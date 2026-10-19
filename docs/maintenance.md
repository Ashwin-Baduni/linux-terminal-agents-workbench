# Maintaining the public setup

The manifest is the export boundary. It names the installed files that can become public source. Agent accounts, conversations, memory notes, histories, backups, machine identifiers and hardware configuration are outside it.

To compare an installed setup with this checkout:

```bash
scripts/import-live.py
scripts/import-live.py --diff
```

Review differences before importing individual files with `--write PATH`. A difference can be an intentional repository fix that the running installation does not have yet. The importer sanitizes only the substitutions recorded in the manifest and checks proposed content before writing. Keep extra private names in a pattern file outside this repository and pass `--patterns /path/to/private-patterns.txt` to both the importer and publication scanner.

After changing source:

```bash
tests/run.py --quick
scripts/check-public.py --history
git diff --check
```

Run `tests/run.py relay` when relay behavior changes and before a release. `tests/clipboard/run.sh` tests the clipboard extension in a throwaway headless GNOME Shell. A test-only extension drives it with a virtual keyboard and pointer, and screenshots are saved. It needs `gnome-shell`, takes a few minutes per screen size and is not part of `tests/run.py`. `WORKBENCH_ONLINE=1 tests/run.py installer` also downloads the official CLI installation scripts to check their interfaces. It does not execute those remote scripts. The normal tests use isolated scratch homes and local stand-ins for system changes and agent processes.

Update the manifests when adding an installed file or dependency. Preserve upstream licences and update `THIRD_PARTY_NOTICES.md` and `manifests/third-party.toml` when changing vendored material. Record real installation results separately from offline tests.

The publication scanner checks the working tree, reachable history and commit identities. It is a pattern check, so inspect the staged diff as well. Use a public or GitHub noreply commit identity before committing. A local commit is not a publication; pushing remains a separate action.

## Reproducing a revision

A Git revision fixes the shipped scripts, templates and vendored source. Ubuntu package repositories, snap channels and vendor CLI releases can change. Existing agent CLIs are kept; `--claude-version` and `--codex-version` choose a release only when the CLI is missing. The installed CLI versions are recorded locally. Ubuntu's package manager may install or update dependencies while satisfying a missing package, and native agent updaters follow their own settings.

Keep a private backup of your memory and other personal state separately. Reinstalling this public repository reconstructs the setup; it does not recover those files.
