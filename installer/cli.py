"""./install.sh: install the Linux Terminal Agents Workbench for this account.

  ./install.sh                       show what an installation would change (same as --check)
  ./install.sh --install             install everything
  ./install.sh --install --only memory,clipboard
                                     install some components (and what they need)
  ./install.sh --rollback last       undo the most recent installation's file and settings changes

Components: terminal, workspace, agents, memory, clipboard, gnome-look.
"""
import argparse
import os
from pathlib import Path
import pwd
import subprocess
import sys

from .engine import Options, Refused, Run, rollback
from .fsops import Unsafe
from .manifest import ManifestError
from .transaction import Interrupted, RollbackRefused


def parse(argv):
    parser = argparse.ArgumentParser(prog='install.sh', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='show the plan without changing anything (default)')
    mode.add_argument('--install', action='store_true', help='install the selected components')
    mode.add_argument('--rollback', metavar='BACKUP',
                      help='restore a backup folder printed by an installation, or "last"')
    parser.add_argument('--keep-changed', action='store_true',
                        help='with --rollback: keep files and settings changed since, restore the rest')
    parser.add_argument('--only', metavar='LIST', help='comma-separated components; their requirements are added')
    parser.add_argument('--target-home', type=Path, metavar='DIR',
                        help="stage into a separate folder instead of your home: files only, nothing on this system")
    parser.add_argument('--no-activate', action='store_true',
                        help='files only: no packages, agent CLIs, GNOME settings or services')
    parser.add_argument('--agent-policy', choices=['full-access'],
                        help="also turn off Claude Code's and Codex's approval prompts (your current policy stays otherwise)")
    parser.add_argument('--claude-version', metavar='RELEASE', help='for a new Claude Code install: latest (default), stable or x.y.z')
    parser.add_argument('--codex-version', metavar='RELEASE', help='for a new Codex install: latest (default) or x.y.z')
    return parser, parser.parse_args(argv)


def main(argv=None):
    parser, args = parse(sys.argv[1:] if argv is None else argv)
    account = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    home = (args.target_home or account).expanduser().resolve()
    real = args.target_home is None      # an explicit --target-home always means staging
    activate = real and not args.no_activate
    components = [c.strip() for c in args.only.split(',') if c.strip()] if args.only else None
    options = Options(home=home, real=real, components=components, agent_policy=args.agent_policy,
                      versions={k: v for k, v in (('claude', args.claude_version), ('codex', args.codex_version)) if v},
                      packages=activate, agents=activate, desktop=activate)
    try:
        if args.target_home is not None:
            if account.is_relative_to(home):
                raise Refused(f'--target-home {home} is your own home or contains it; staging needs a separate '
                              'folder. To install for yourself, leave --target-home out.')
            if not home.is_dir():
                raise Refused(f'{home} does not exist; create the staging folder first.')
        if args.keep_changed and not args.rollback:
            raise Refused('--keep-changed goes with --rollback.')
        if args.rollback:
            return rollback(options, args.rollback, keep_changed=args.keep_changed)
        run = Run(options)
        return run.install() if args.install else run.check()
    except (Refused, RollbackRefused, Interrupted, ManifestError, Unsafe, ValueError) as error:
        print(f'install.sh: {error}', file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as error:
        print(f'install.sh: {" ".join(map(str, error.cmd))} failed (exit {error.returncode}).', file=sys.stderr)
        return 1
    except (RuntimeError, OSError) as error:
        print(f'install.sh: {error}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('\ninstall.sh: interrupted.', file=sys.stderr)
        return 130


if __name__ == '__main__':
    sys.exit(main())
