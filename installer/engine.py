"""Check, install and roll back: the steps behind ./install.sh."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import pwd
import re
import shutil

from . import packages, preflight
from .desktop import Desktop, extensions_with_schema, refresh_caches
from .fsops import Home, Unsafe
from .manifest import load
from .plan import Context, PlanError, build, describe
from .stage import SAFE_HOME
from .transaction import (STATE_DIR, RollbackRefused, Transaction, install_lock, latest, load_journal,
                          rollback as undo, unfinished)

SAFE_VALUE = re.compile(r'/[A-Za-z0-9_./+-]+')


class Refused(RuntimeError):
    """The installer stopped before changing anything."""


@dataclass
class Options:
    home: Path
    real: bool                          # the home is the account's own home
    components: list | None = None
    agent_policy: str | None = None
    versions: dict = field(default_factory=dict)
    packages: bool = False              # install missing Ubuntu packages and snaps
    agents: bool = False                # install missing agent CLIs
    desktop: bool = False               # GNOME settings, user units and caches


def account_home():
    return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()


def resolve_values(home, real):
    """Placeholder values for the target user, never another account's paths."""
    claude = packages.find('claude', home, real) or str(home / '.local/bin/claude')
    ghostty = (shutil.which('ghostty') if real else None) or '/usr/bin/ghostty'
    values = {'HOME': str(home), 'CLAUDE_BIN': claude, 'GHOSTTY_BIN': ghostty}
    for name, value in values.items():
        if not SAFE_VALUE.fullmatch(value):
            raise Refused(f'{name} resolves to {value!r}, which cannot be written unquoted into desktop entries.')
    return values


def desktop_operations(manifest, chosen, home):
    operations = []
    for name in chosen:
        component = manifest.components[name]
        if 'gsettings' in component:
            data = json.loads((manifest.repo / component['gsettings']).read_text())
            for op in ('set', 'add', 'remove'):
                operations += [(op, *row) for row in data.get(op, [])]
        if 'extension' in component:
            uuid, schema = component['extension'], component['settings-schema']
            others = extensions_with_schema(home, schema, uuid)
            if others:
                operations.append(('remove', 'org.gnome.shell', 'enabled-extensions', others))
            operations.append(('remove', 'org.gnome.shell', 'disabled-extensions', [uuid]))
    return operations


def units_for(manifest, chosen):
    return [unit for name in chosen for unit in manifest.components[name].get('units', [])]


class Run:
    """One check or installation."""

    def __init__(self, options, log=print, desktop=None):
        self.options = options
        self.log = log
        self.manifest = load()
        if not SAFE_HOME.fullmatch(str(Path(options.home).resolve())):
            raise Refused(f'{options.home}: use a home path without spaces or shell metacharacters.')
        if not options.real and account_home().is_relative_to(Path(options.home).resolve()):
            raise Refused(f'{options.home} is your own home or contains it; staging needs a separate folder.')
        self.home = Home(options.home)
        self.chosen = self.manifest.closure(options.components or self.manifest.default)
        self.desktop = desktop or (Desktop() if options.desktop else None)
        self.agents = packages.agents_from(self.manifest.components['agents'], options.versions) \
            if 'agents' in self.chosen else []

    # --- facts ---------------------------------------------------------------
    def platform_problems(self):
        return preflight.problems(self.manifest.supported, preflight.facts()) if self.options.real else []

    def wanted_packages(self):
        apt, snaps = [], []
        for name in self.chosen:
            apt += self.manifest.components[name].get('apt', [])
            snaps += self.manifest.components[name].get('snap', [])
        return apt, snaps

    def missing(self):
        if not self.options.packages:
            return [], []
        apt, snaps = self.wanted_packages()
        return packages.missing_apt(apt), packages.missing_snaps(snaps, self.home.root, self.options.real)

    def missing_agents(self):
        if not self.options.agents:
            return []
        return [agent for agent in self.agents if not packages.find(agent.name, self.home.root, self.options.real)]

    def plan(self):
        values = resolve_values(self.home.root, self.options.real)
        context = Context(home=self.home, values=values, real=self.options.real, env=dict(os.environ),
                          agent_policy=self.options.agent_policy)
        try:
            return build(self.manifest, context, self.chosen)
        except (PlanError, Unsafe) as error:
            raise Refused(str(error)) from error

    def desktop_steps(self, plan):
        if not self.desktop:
            return [], [], []
        settings, skipped = self.desktop.plan_settings(desktop_operations(self.manifest, self.chosen, self.home.root))
        changed = {c.path for c in plan.changes}
        names = units_for(self.manifest, self.chosen)
        restart = {name for name in names if f'.config/systemd/user/{name}' in changed or
                   (name.endswith('.timer') and f'.config/systemd/user/{name[:-6]}.service' in changed)}
        if changed & {'.local/share/workspace/workspace.py', '.local/bin/workspace-control'}:
            restart.add('workspace-monitor.service')
        units = self.desktop.plan_units(names, restart)
        return settings, units, skipped

    # --- check ---------------------------------------------------------------
    def check(self):
        log = self.log
        log(f'Target: {self.home.root}' + ('' if self.options.real else ' (a staging folder: files only)'))
        log('Components: ' + ', '.join(self.chosen))
        problems = self.platform_problems()
        for path, journal in unfinished(self.home):
            log(f'An earlier installation did not finish: ./install.sh --rollback {path}')
        apt, snaps = self.missing()
        if apt:
            log('Ubuntu packages to install: ' + ' '.join(apt))
        if snaps:
            log('Snaps to install: ' + ', '.join(s['name'] for s in snaps))
        for agent in self.missing_agents():
            log(f'{agent.name}: would download {agent.installer} and run it for release "{agent.version}"')
        for agent in self.agents:
            found = packages.find(agent.name, self.home.root, self.options.real)
            if found:
                log(f'{agent.name}: keeping {found}')
        plan = self.plan()
        settings, units, skipped = self.desktop_steps(plan)
        self.report_plan(plan, settings, units, skipped)
        if problems:
            log('\nThis system is not the tested target, so --install would stop here:')
            for reason in problems:
                log('  ' + reason)
            return 2
        log('\nNothing was changed. Run with --install to apply this.')
        return 0

    def report_plan(self, plan, settings, units, skipped):
        log = self.log
        if plan.changes:
            log(f'\nFiles ({len(plan.changes)} changes):')
            for line in describe(plan):
                log(line)
        else:
            log('\nFiles: everything is already in place.')
        for path, reason in plan.kept:
            log(f'  kept           ~/{path} ({reason})')
        for step in settings:
            log(f'  GNOME          {step["schema"]} {step["key"]}: {step["before"]} → {step["after"]}')
        for name in skipped:
            log(f'  GNOME          {name}: not on this desktop, skipped')
        for step in units:
            log(f'  service        {step["unit"]}: ' + ('restart' if step['after'].get('restart') else 'enable and start'))
        for note in plan.notes:
            log('  note: ' + note)

    # --- install -------------------------------------------------------------
    def install(self):
        log = self.log
        problems = self.platform_problems()
        if problems:
            raise Refused('This system is not the tested target, so nothing was changed:\n'
                          + '\n'.join('  ' + p for p in problems))
        with install_lock(self.home):
            stuck = unfinished(self.home)
            if stuck:
                raise Refused('An earlier installation did not finish. Restore it first with\n'
                              f'  ./install.sh --rollback {stuck[-1][0]}')
            # Settle every file change first: a broken setting or an unsafe symlink stops the
            # installation before any package or agent CLI is installed.
            self.plan()
            apt, snaps = self.missing()
            packages.install_apt(apt, log)
            packages.install_snaps(snaps, log)
            versions = self.install_agents()
            plan = self.plan()        # again, now that the agent CLIs' paths are known
            settings, units, skipped = self.desktop_steps(plan)
            self.report_plan(plan, settings, units, skipped)
            if not (plan.changes or settings or units):
                installed = apt or snaps or any(info['installed'] for info in versions.values())
                log('\nFiles and settings were already in place.' if installed
                    else '\nEverything is already installed; nothing was changed.')
                self.final_notes(plan, None, versions)
                return 0
            meta = {'components': self.chosen, 'agent_policy': self.options.agent_policy, 'agents': versions,
                    'packages_installed': apt + [s['name'] for s in snaps]}
            transaction = Transaction(self.home, plan, self.desktop, meta)
            folder = transaction.prepare(settings, units)
            try:
                transaction.apply()
            except BaseException:
                self.report_failure(transaction.journal, folder)
                raise
            if self.options.desktop:
                for cache in refresh_caches(self.home.root, None, {c.path for c in plan.changes}):
                    log(f'Refreshed the {cache}.')
            log(f'\nInstalled. Backup and journal: {folder}')
            self.final_notes(plan, folder, versions)
            return 0

    def report_failure(self, journal, folder):
        recovery = journal.get('recovery', {})
        log = self.log
        if journal.get('status') == 'rolled-back':
            log(f'\nThe installation failed ({journal.get("error")}); everything it had changed was put back.')
            for item in recovery.get('kept', []):
                log('  left in place: ' + item)
        else:
            log(f'\nThe installation failed ({journal.get("error")}) and could not undo everything.')
            for problem in recovery.get('problems', []):
                log('  not undone: ' + problem)
            if recovery.get('undo_error'):
                log('  the undo itself failed: ' + recovery['undo_error'])
            log('Changes made by you or another program were kept. Review them, then run\n'
                f'  ./install.sh --rollback {folder}\n'
                'Add --keep-changed to keep those changes and restore everything else.')
        log(f'Details: {folder}/journal.json')

    def install_agents(self):
        versions = {}
        for agent in self.agents:
            found = packages.find(agent.name, self.home.root, self.options.real)
            if not found and self.options.agents:
                self.home.mkdir(STATE_DIR / 'downloads', 0o700)
                downloads = self.home.root / STATE_DIR / 'downloads'
                self.log(f'Installing {agent.name} with its official installer ({agent.docs}):')
                found, version = packages.install_agent(agent, self.home.root, downloads, self.log)
                versions[agent.name] = {'path': found, 'version': version, 'installed': True,
                                        'release': agent.version, 'installer': agent.installer}
            elif found:
                versions[agent.name] = {'path': found, 'version': packages.version_of(found), 'installed': False}
        return versions

    def record(self, versions):
        """Keep what this installation used and found in ~/.local/state/workbench/installed.json."""
        record = {'installed': datetime.now(timezone.utc).isoformat(), 'components': self.chosen,
                  'agent_policy': self.options.agent_policy, 'agents': versions}
        self.home.write(STATE_DIR / 'installed.json', (json.dumps(record, indent=2) + '\n').encode(), 0o600)

    def final_notes(self, plan, folder, versions):
        log = self.log
        if self.options.real:
            self.record(versions)
        changed = {c.path for c in plan.changes}
        for name, info in versions.items():
            log(f'{name}: {info["version"]} at {info["path"]}' + (' (installed now)' if info['installed'] else ''))
        notes = []
        if not self.options.real:
            notes.append(f'Staged into {self.home.root}; nothing else on this system was changed.')
        elif '.bashrc' in changed or '.inputrc' in changed:
            notes.append('Open a new terminal window: the shell setup loads in new shells only.')
        if self.options.real and any('gnome-shell/extensions' in p for p in changed):
            notes.append('Log out and back in once: GNOME loads a newly installed extension (Super+V) at login.')
        if self.options.real and 'agents' in self.chosen:
            notes.append('Sign in to each agent yourself: run `claude`, then `codex`, once each.')
        if not self.options.desktop and self.options.real:
            notes.append('--no-activate: packages, GNOME settings and services were left alone.')
        for note in notes:
            log('• ' + note)
        if folder:
            log(f'To undo the file and settings changes: ./install.sh --rollback {folder}')
            log('A rollback does not remove installed packages, agent CLIs or sign-ins, and it keeps '
                'memory notes you have written since.')


def rollback(options, folder, log=print, desktop=None, keep_changed=False):
    home = Home(options.home)
    with install_lock(home):
        target = latest(home) if folder in ('last', 'latest') else Path(folder).expanduser().absolute()
        _, journal = load_journal(home, target)
        needs_desktop = any(step['type'] != 'path' for step in journal.get('steps', []))
        if needs_desktop and not desktop:
            if not options.real:
                raise Refused('This backup also changed GNOME settings and services; roll it back from that account.')
            desktop = Desktop()
        outcome = undo(home, target, desktop, keep_changed=keep_changed)
        log(f'Rolled back {target}: {len(outcome.restored)} item(s) restored.')
        for item in outcome.left:
            log('  left in place: ' + item)
        log('Installed packages, agent CLIs, sign-ins and memory notes written since are not part of a rollback.')
        return 0


__all__ = ['Options', 'Run', 'Refused', 'RollbackRefused', 'rollback']
