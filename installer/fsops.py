"""File operations confined to one home folder.

Every write walks from the home down with O_NOFOLLOW, so a symlinked folder can never
carry a write outside the home, even if it appears after the plan was made. A folder
symlink that stays inside the home is allowed: paths are resolved first, and the walk
then uses the resolved path. A symlink at the destination itself is replaced, never
written through.
"""
from dataclasses import dataclass
import errno
import hashlib
import os
from pathlib import Path, PurePosixPath
import secrets
import stat

DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class Unsafe(ValueError):
    """A destination would leave the home, or something unexpected stands in its way."""


@dataclass(frozen=True)
class State:
    """What is at a path, without following a symlink there."""
    kind: str                  # absent, file, symlink, dir, other
    mode: int | None = None
    sha256: str | None = None
    link: str | None = None

    def as_dict(self):
        return {k: v for k, v in self.__dict__.items() if v is not None}

    @classmethod
    def from_dict(cls, data):
        return cls(**data)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def relative(home, path):
    """path as a relative PurePosixPath inside home; refuses anything that climbs out."""
    pure = PurePosixPath(os.path.relpath(path, home)) if Path(path).is_absolute() else PurePosixPath(path)
    if pure.is_absolute() or not pure.parts or '..' in pure.parts:
        raise Unsafe(f'{path} is not inside {home}')
    return pure


def real_parent(home, rel):
    """The resolved folder that will hold rel, checked to stay inside home."""
    home = Path(home)
    current = home
    for part in rel.parts[:-1]:
        current = current / part
        if current.is_symlink():
            target = current.resolve()
            if not target.is_relative_to(home):
                raise Unsafe(f'{current} is a symlink to {target}, outside {home}; '
                             'the installer will not write through it.')
            current = target
        elif current.exists() and not current.is_dir():
            raise Unsafe(f'{current} is in the way: a folder is needed there.')
    return relative(home, current) if current != home else PurePosixPath('.')


def _open_dir(home, rel_dir, create, created):
    fd = os.open(home, DIR_FLAGS)
    try:
        prefix = PurePosixPath('.')
        for part in rel_dir.parts:
            if part == '.':
                continue
            prefix = prefix / part
            try:
                child = os.open(part, DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, 0o755, dir_fd=fd)
                created.append(str(prefix))
                child = os.open(part, DIR_FLAGS, dir_fd=fd)
            except OSError as error:
                if error.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise Unsafe(f'{Path(home) / prefix} changed into a symlink or file; stopping.') from error
                raise
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


class Home:
    """Safe reads and writes under one resolved home folder."""

    def __init__(self, home):
        self.root = Path(home).resolve()
        self.created = []          # folders made by writes, in creation order (relative paths)

    def path(self, rel):
        return self.root / relative(self.root, rel)

    def locate(self, rel, create=False):
        """(folder fd, leaf name) for rel; the caller closes the fd."""
        rel = relative(self.root, rel)
        parent = real_parent(self.root, rel)
        fd = _open_dir(self.root, parent, create, self.created)
        return fd, rel.name

    def state(self, rel):
        try:
            fd, leaf = self.locate(rel)
        except FileNotFoundError:
            return State('absent')
        try:
            try:
                info = os.stat(leaf, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                return State('absent')
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISLNK(info.st_mode):
                return State('symlink', link=os.readlink(leaf, dir_fd=fd))
            if stat.S_ISDIR(info.st_mode):
                return State('dir', mode=mode)
            if stat.S_ISREG(info.st_mode):
                return State('file', mode=mode, sha256=sha(self._read(fd, leaf)))
            return State('other', mode=mode)
        finally:
            os.close(fd)

    @staticmethod
    def _read(fd, leaf):
        handle = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
        with os.fdopen(handle, 'rb') as stream:
            return stream.read()

    def read(self, rel):
        """A regular file's bytes, never through a symlink at rel."""
        fd, leaf = self.locate(rel)
        try:
            return self._read(fd, leaf)
        finally:
            os.close(fd)

    def read_followed(self, rel):
        """The bytes a program would read at rel, following a final symlink (for merging only)."""
        current = self.state(rel)
        if current.kind == 'file':
            return self.read(rel)
        if current.kind == 'symlink':
            try:
                return self.path(rel).read_bytes()
            except OSError:
                return None
        return None

    def write(self, rel, data, mode):
        """Replace whatever is at rel (a symlink there is replaced, not followed) with a regular file."""
        fd, leaf = self.locate(rel, create=True)
        temporary = f'.{leaf}.workbench-{secrets.token_hex(4)}'
        try:
            handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=fd)
            try:
                with os.fdopen(handle, 'wb') as stream:
                    stream.write(data)
                    stream.flush()
                    os.fchmod(stream.fileno(), mode)
                    os.fsync(stream.fileno())
                os.rename(temporary, leaf, src_dir_fd=fd, dst_dir_fd=fd)
            except BaseException:
                try:
                    os.unlink(temporary, dir_fd=fd)
                except FileNotFoundError:
                    pass
                raise
            os.fsync(fd)
        finally:
            os.close(fd)

    def symlink(self, rel, target):
        fd, leaf = self.locate(rel, create=True)
        temporary = f'.{leaf}.workbench-{secrets.token_hex(4)}'
        try:
            os.symlink(target, temporary, dir_fd=fd)
            try:
                os.rename(temporary, leaf, src_dir_fd=fd, dst_dir_fd=fd)
            except BaseException:
                os.unlink(temporary, dir_fd=fd)
                raise
        finally:
            os.close(fd)

    def mkdir(self, rel, mode=0o755):
        fd, leaf = self.locate(rel, create=True)
        try:
            os.mkdir(leaf, mode, dir_fd=fd)
        except FileExistsError:
            if not stat.S_ISDIR(os.stat(leaf, dir_fd=fd, follow_symlinks=False).st_mode):
                raise Unsafe(f'{self.root / rel} exists and is not a folder.')
        finally:
            os.close(fd)

    def remove(self, rel):
        """Remove a file, symlink or empty folder at rel; True when something was removed."""
        try:
            fd, leaf = self.locate(rel)
        except FileNotFoundError:
            return False
        try:
            info = os.stat(leaf, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                try:
                    os.rmdir(leaf, dir_fd=fd)
                except OSError as error:
                    if error.errno == errno.ENOTEMPTY:
                        return False
                    raise
            else:
                os.unlink(leaf, dir_fd=fd)
            return True
        except FileNotFoundError:
            return False
        finally:
            os.close(fd)

    def chmod(self, rel, mode):
        """Change a regular file's or folder's mode; a symlink at rel is refused, not followed."""
        fd, leaf = self.locate(rel)
        try:
            try:
                handle = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            except OSError as error:
                if error.errno == errno.ELOOP:
                    raise Unsafe(f'{self.root / rel} is a symlink; its mode is left alone.') from error
                raise
            try:
                os.fchmod(handle, mode)
            finally:
                os.close(handle)
        finally:
            os.close(fd)

    def link_text(self, rel, target):
        """What a symlink at rel must contain to reach the home path target.

        The system resolves a relative link from the folder the link really lives in, so
        the text is computed from resolved folders: a symlinked ~/.codex or
        ~/.config/yazi-sidebar still gets a link that reaches the intended file.
        """
        rel, target = relative(self.root, rel), relative(self.root, target)
        here = self.root / real_parent(self.root, rel)
        there = self.root / real_parent(self.root, target) / target.name
        return os.path.relpath(there, here)

    def missing_folders(self, rel):
        """The folders a write to rel would create, outermost first (after resolving inside-home links)."""
        rel = relative(self.root, rel)
        parent = real_parent(self.root, rel)
        missing, current = [], self.root
        for part in parent.parts:
            if part == '.':
                continue
            current = current / part
            if not current.exists() and not current.is_symlink():
                missing.append(str(relative(self.root, current)))
        return missing
