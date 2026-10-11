# Verification record — 2026-10-11

The installer and desktop rehearsal below used the code in `78f08a8`. Later handoff documentation does not change that code. The source machine's terminal, desktop, agent accounts and memory were not modified by installation tests.

## Automated checks

| Suite | Result |
|---|---|
| Repository and manifest | 29 passed |
| Publication scanner regressions | 11 passed |
| Installer and recovery | 63 passed, including the online vendor-interface check |
| Shared memory | 32 passed |
| Copy everything | 12 passed |
| Workspace scenarios | 9 passed |
| Offline relay | 73 passed |

The final quick run used `WORKBENCH_ONLINE=1 tests/run.py --quick`. Without that variable, one installer test is skipped. That test downloads the official installation scripts and checks their interfaces without executing them. All normal suites install this checkout into scratch homes; they do not test against a contributor's live files.

The relay suite was run at `03c5d86`. Its source, workspace integration and test harness are unchanged in `78f08a8`. It uses local stand-ins for agent processes and exercises delivery, retries, recovery and termination without paid model calls.

Publication checks covered the working tree, every reachable commit and commit identity, with additional private-name patterns kept outside the repository. Commit identities use GitHub noreply addresses. A scan supplements inspection of the source and staged diff; it cannot prove that arbitrary text is non-sensitive.

## Real desktop rehearsal

A disposable KVM guest used the [official Ubuntu 26.04 cloud image](https://cloud-images.ubuntu.com/releases/26.04/release/) and Ubuntu's GNOME desktop packages. The image's SHA-256 was checked against its published checksum:

```text
8800651811af9a85465ad1d552add729947bb16488dddb4a9b5305a3d97332b2
```

The guest had a fresh home for the synthetic user `reviewer`, 4 CPU cores, 4 GiB RAM and a disk-backed 48 GiB virtual disk. It ran a real GNOME Wayland login with its own user systemd manager. Only public source and synthetic test fixtures were copied in. No account credentials, personal memory or live configuration were imported.

| Software | Observed version |
|---|---|
| Ubuntu / GNOME Shell | 26.04 / 50.1 |
| Kernel / Python | 7.0.0-34-generic / 3.14.4 |
| Ghostty package | 1.3.0~us1-0ubuntu1.1 |
| JetBrains Mono package | 2.304+ds-6 |
| tmux package | 3.6a-2ubuntu0.1 |
| Yazi snap | 26.9.1, revision source 8dd895c6 |
| Claude Code | 2.1.296 |
| Codex CLI | 0.162.1 |

These are observed versions, not version pins. Package repositories, snap channels and vendor release defaults can change.

The rehearsal passed these checks:

1. `--check` planned the complete setup. `--install` installed real Ubuntu packages, the Yazi snap and both official native agent CLIs, then applied files, GNOME settings and services. Both CLIs successfully reported their versions.
2. After rebooting into GNOME, a second `--install` reported that everything was already installed, with no file, setting or service changes. The monitor and both timers were enabled and active.
3. Ghostty displayed the pure black theme, JetBrains Mono, pane borders and file icons. The Directory sidebar ran Yazi and listed project files. Right-click opened the workspace menu; clicking its layout/output entry opened the corresponding dialog.
4. The clipboard extension reported `ACTIVE`. Super+V displayed copied text, selecting an entry made that text available through `wl-paste`, and Copy everything reached the real Wayland clipboard.
5. `ai-memory link` registered a project. Renaming and relinking its folder preserved the memory name and a synthetic note. Claude's local memory setting and Codex's global link pointed to the shared memory. A snapshot committed locally; `git fsck --full` passed and the memory repository had no remotes.
6. Park/reopen preserved the terminal process IDs. Saving a layout, stopping the workspace tmux server and restoring recovered the two agent pane labels and the Directory sidebar.
7. A deliberate configuration edit made rollback refuse before restoring anything. `--rollback last --keep-changed` then restored backed-up file contents and modes, all recorded GNOME values and the installed services. It preserved the edit, newly written memory, its local Git history, Ubuntu packages, Yazi and both agent executables.
8. Installing again after rollback succeeded and preserved the project memory. The final `--check` planned no changes. The guest's package audit was clean and the final VM run recorded no container memory-limit events.

## What still requires the user's account or hardware

Agent authentication and a relay between signed-in agents were not exercised in the guest. Sign in to both CLIs after installation, then start a pair from the workspace menu. The offline relay tests cover its protocol; they do not verify a particular account, subscription, model availability or vendor authentication flow.

Physical OLED appearance, GPU acceleration, HDR, monitor scaling, microphone hardware and remote access were not tested. Those machine-specific settings are outside this installer. This rehearsal is an Ubuntu desktop assembled from official packages in a VM, not a physical-machine reinstall from the desktop ISO.

For a future release, repeat the automated suites and this desktop sequence on a fresh disposable account. Keep private memory and sign-ins out of public evidence.
