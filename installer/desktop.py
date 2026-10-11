"""GNOME settings, systemd user units and desktop caches, for a real installation only."""
import ast
import json
from pathlib import Path
import re
import shutil
import subprocess

TYPE_PREFIX = re.compile(r'^(?:@[a-z]+|u?int(?:16|32|64)|byte|double|boolean|string|objectpath)\s+')


def parse(value):
    """A gsettings value (GVariant text) as a Python value, for comparing."""
    value = TYPE_PREFIX.sub('', value.strip())
    if value in ('true', 'false'):
        return value == 'true'
    try:
        return ast.literal_eval(value)
    except (ValueError, SyntaxError):
        return value


def text(value):
    """GVariant text for a list of strings."""
    return '@as []' if not value else '[' + ', '.join(repr(str(item)) for item in value) + ']'


class Desktop:
    def __init__(self, env=None, run=subprocess.run):
        self.env = env
        self.run = run
        self.reloaded = False
        self.masked = []        # units plan_units left alone because systemd reports them masked

    def command(self, *args, check=False):
        return self.run([str(a) for a in args], capture_output=True, text=True, env=self.env, check=check)

    # --- GNOME settings -------------------------------------------------------
    def get_value(self, schema, key):
        result = self.command('gsettings', 'get', schema, key)
        return result.stdout.strip() if result.returncode == 0 else None

    def set_value(self, schema, key, value):
        self.command('gsettings', 'set', schema, key, value, check=True)

    @staticmethod
    def same(a, b):
        return parse(a) == parse(b)

    def plan_settings(self, operations):
        """[(op, schema, key, value)] → journal steps {schema, key, before, after} that change something."""
        wanted, skipped = {}, []
        for op, schema, key, value in operations:
            current = wanted.get((schema, key), {}).get('after')
            before = wanted.get((schema, key), {}).get('before')
            if current is None:
                before = self.get_value(schema, key)
                if before is None:
                    skipped.append(f'{schema} {key}')
                    continue
                current = before
            if op == 'set':
                after = value
            else:
                items = parse(current)
                if not isinstance(items, list):
                    skipped.append(f'{schema} {key} (not a list)')
                    continue
                if op == 'add':
                    after = text(items + [v for v in value if v not in items])
                else:
                    after = text([v for v in items if v not in value])
            wanted[(schema, key)] = {'schema': schema, 'key': key, 'before': before, 'after': after}
        steps = [step for step in wanted.values() if not self.same(step['before'], step['after'])]
        return steps, skipped

    # --- systemd user units ---------------------------------------------------
    def unit_state(self, unit):
        enabled = self.command('systemctl', '--user', 'is-enabled', unit).stdout.strip() or 'unknown'
        active = self.command('systemctl', '--user', 'is-active', unit).stdout.strip() or 'unknown'
        return {'enabled': enabled, 'active': active}

    def session_environment(self):
        """The variables the systemd user manager (and so the desktop session's services) has."""
        output = self.command('systemctl', '--user', 'show-environment').stdout
        return dict(line.split('=', 1) for line in output.splitlines() if '=' in line)

    def reload(self, force=False):
        """Make systemd read the unit files; force after a rollback has put old files back."""
        if force or not self.reloaded:
            self.command('systemctl', '--user', 'daemon-reload', check=True)
            self.reloaded = True

    def plan_units(self, units, restart=()):
        steps, self.masked = [], []
        for unit in units:
            before = self.unit_state(unit)
            if before['enabled'].startswith('masked'):
                self.masked.append(unit)
                continue
            after = {'enabled': 'enabled', 'active': 'active'}
            if before == after and unit not in restart:
                continue
            steps.append({'unit': unit, 'before': before, 'after': {**after, 'restart': unit in restart}})
        return steps

    def set_unit(self, unit, after):
        self.reload()
        self.command('systemctl', '--user', 'enable', unit, check=True)
        self.command('systemctl', '--user', 'restart' if after.get('restart') else 'start', unit, check=True)

def extensions_with_schema(home, schema, exclude):
    """UUIDs of other installed GNOME extensions that declare the same settings schema."""
    found = []
    for root in (Path(home) / '.local/share/gnome-shell/extensions', Path('/usr/share/gnome-shell/extensions')):
        if not root.is_dir():
            continue
        for metadata in sorted(root.glob('*/metadata.json')):
            try:
                data = json.loads(metadata.read_text())
            except (OSError, ValueError):
                continue
            if data.get('settings-schema') == schema and data.get('uuid') not in (exclude, None):
                found.append(data['uuid'])
    return found


def refresh_caches(home, env, changed):
    """Rebuild what GNOME and bat cache from the installed files."""
    done = []
    if any(p.startswith('.config/bat/') for p in changed) and shutil.which('batcat'):
        subprocess.run(['batcat', 'cache', '--build'], capture_output=True, env=env)
        done.append('bat theme cache')
    if any(p.startswith('.local/share/applications/') for p in changed) and shutil.which('update-desktop-database'):
        subprocess.run(['update-desktop-database', str(Path(home) / '.local/share/applications')],
                       capture_output=True, env=env)
        done.append('desktop entries')
    return done
