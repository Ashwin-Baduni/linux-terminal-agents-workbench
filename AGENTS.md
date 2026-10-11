# Working on this repository

This is the public source of an Ubuntu terminal and agent workspace installer. Private notes, credentials, conversations and installed runtime state belong outside the repository.

- Read `manifests/components.toml` and `manifests/files.toml` before changing the installed layout. Keep every shipped file accounted for, including third-party licences.
- Test installation in a fresh scratch home with `--target-home`. Do not apply the installer, change desktop settings or start agent sessions in a contributor's real home as part of a test.
- Run `tests/run.py --quick` for installer and configuration changes. Run the relay suite when relay behavior changes or for a release verification. Tests must exercise this checkout, never fall back to the live installation.
- Preserve unrelated configuration and private memory. Journal changes before mutation and exercise failure recovery when changing transaction code.
- Run `scripts/check-public.py --history` before preparing a public revision. Keep any extra private scan patterns outside the repository. Inspect the diff too; a pattern scan cannot recognize every secret.
- Keep installation instructions and `SUMMARY.md` consistent with verified behavior. Clearly distinguish offline tests from a real desktop rehearsal.
