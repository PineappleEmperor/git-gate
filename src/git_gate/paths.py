"""Workspace path checks that hold against symlink swaps.

Every check opens the object first and then asks the kernel where the opened object really is
(/proc/self/fd), so a swap between check and use cannot move the target.
"""

import os
import secrets
import stat
from contextlib import contextmanager, suppress
from pathlib import Path

from git_gate import Refused
from git_gate.config import Rules

MAX_COPY = 1 << 20
CHUNK = 1 << 20


def fd_path(fd: int) -> Path:
    return Path(os.readlink(f"/proc/self/fd/{fd}"))


def check_inside(rules: Rules, real: Path, what: str) -> None:
    for f in rules.forbidden:
        if real.is_relative_to(f):
            raise Refused(f"{what} {real} is inside gate-owned {f}.")
    if not any(real.is_relative_to(w) for w in rules.workspaces):
        raise Refused(f"{what} {real} is outside the workspaces in rules.toml.")


def require_absolute(path: str, what: str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        raise Refused(f"{what} must be an absolute path, got {path!r}.")
    return p


@contextmanager
def open_dir(rules: Rules, path: str | Path, what: str = "Directory"):
    """Yield (fd, real path) of a workspace directory. The final component may not be a symlink."""
    p = require_absolute(str(path), what)
    try:
        fd = os.open(p, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as e:
        raise Refused(f"{what} {p}: {e.strerror}.") from None
    try:
        real = fd_path(fd)
        check_inside(rules, real, what)
        yield fd, real
    finally:
        os.close(fd)


def read_workspace_file(rules: Rules, path: str, what: str = "File") -> bytes:
    p = require_absolute(path, what)
    try:
        fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as e:
        raise Refused(f"{what} {p}: {e.strerror}.") from None
    with os.fdopen(fd, "rb") as f:
        check_inside(rules, fd_path(fd), what)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Refused(f"{what} {p} is not a regular file.")
        data = f.read(MAX_COPY + 1)
    if len(data) > MAX_COPY:
        raise Refused(f"{what} {p} is larger than {MAX_COPY} bytes.")
    return data


def copy_in(rules: Rules, path: str | Path, dst: Path, limit: int, what: str = "File") -> None:
    """Stream a workspace regular file into a new gate-owned file `dst`, refusing past `limit` bytes.

    O_NONBLOCK means a FIFO or device is opened at once and then refused, never waited on.
    """
    p = require_absolute(str(path), what)
    try:
        fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as e:
        raise Refused(f"{what} {p}: {e.strerror}.") from None
    try:
        check_inside(rules, fd_path(fd), what)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise Refused(f"{what} {p} is not a regular file.")
        if st.st_size > limit:
            raise Refused(f"{what} {p} is larger than {limit} bytes.")
    except BaseException:
        os.close(fd)
        raise
    with os.fdopen(fd, "rb") as f:
        out = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(out, "wb") as o:
                copied = 0
                while chunk := f.read(min(CHUNK, limit + 1 - copied)):
                    copied += len(chunk)
                    if copied > limit:
                        raise Refused(f"{what} {p} is larger than {limit} bytes.")
                    o.write(chunk)
        except BaseException:
            dst.unlink(missing_ok=True)
            raise


def write_out(rules: Rules, path: str | Path, src: Path, what: str = "File") -> None:
    """Stream gate-owned `src` to a workspace path, replacing a regular file there and nothing else.

    The data goes to a fresh name in the opened parent directory and is renamed over the target,
    so a symlink or hard link at the target is replaced, never written through.
    """
    p = require_absolute(str(path), what)
    name = p.name
    if name in ("", ".", "..") or p != p.parent / name:
        raise Refused(f"{what} {p} does not name a file.")
    with open_dir(rules, p.parent, f"{what} directory") as (dfd, real):
        _require_regular_or_absent(dfd, name, real / name, what)
        tmp = f".{name}.git-gate-{secrets.token_hex(8)}"
        try:
            out = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK, 0o644, dir_fd=dfd)
        except OSError as e:
            raise Refused(f"Cannot write {what.lower()} into {real}: {e.strerror}.") from None
        try:
            with os.fdopen(out, "wb") as o, open(src, "rb") as f:
                while chunk := f.read(CHUNK):
                    o.write(chunk)
            _require_regular_or_absent(dfd, name, real / name, what)
            os.rename(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        except OSError as e:
            raise Refused(f"Cannot write {what.lower()} {real / name}: {e.strerror}.") from None
        finally:
            with suppress(FileNotFoundError):
                os.unlink(tmp, dir_fd=dfd)


def _require_regular_or_absent(dir_fd: int, name: str, shown: Path, what: str) -> None:
    try:
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(st.st_mode):
        raise Refused(f"{what} {shown} exists and is not a regular file.")


def copy_tree_into(src: Path, dst_fd: int, clobber: bool) -> list[str]:
    """Copy regular files and dirs from a gate-owned tree into an open directory handle."""
    written = []
    for entry in sorted(os.scandir(src), key=lambda e: e.name):
        if entry.is_symlink():
            continue
        if entry.is_dir():
            with suppress(FileExistsError):
                os.mkdir(entry.name, 0o755, dir_fd=dst_fd)
            sub = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dst_fd)
            try:
                written += [f"{entry.name}/{n}" for n in copy_tree_into(Path(entry.path), sub, clobber)]
            finally:
                os.close(sub)
        elif entry.is_file():
            write_file_at(dst_fd, entry.name, Path(entry.path).read_bytes(), clobber)
            written.append(entry.name)
    return written


def write_file_at(dir_fd: int, name: str, data: bytes, clobber: bool) -> None:
    if clobber:
        try:
            st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            if not stat.S_ISREG(st.st_mode):
                raise Refused(f"{name} exists in the target and is not a regular file.")
            os.unlink(name, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=dir_fd)
    except FileExistsError:
        raise Refused(f"{name} already exists in the target; pass --clobber to replace it.") from None
    with os.fdopen(fd, "wb") as f:
        f.write(data)
