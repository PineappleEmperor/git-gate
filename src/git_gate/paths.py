"""Workspace path checks that hold against symlink swaps, and the exchange contract.

Every check opens the object first and then asks the kernel where the opened object really is
(/proc/self/fd), so a swap between check and use cannot move the target.

The gate runs outside the sandbox, which masks secrets inside workspaces (.env, secrets.yaml,
.storage) and write-protects config that runs unsandboxed (.mcp.json, .claude/, .vscode/). The
gate cannot read that configuration, so every file it exchanges with an agent follows one fixed
contract instead, so it never reads or writes what the agent itself could not:

- the real path is inside a workspace and outside the gate's own directories;
- no component of it below the workspace root starts with ".", except one named exactly ".tmp";
- the name has the purpose's suffix: *.bundle for bundles, *.md or *.txt for body files;
- reads take a regular file with one link only; writes replace only an absent name or such a file.
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
        if rules.session:
            raise Refused(f"{what} {real} is outside this session's folder, {rules.session.folder}.")
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


SCRATCH = ".tmp"
EXCHANGE = {"bundle": ("Bundle", (".bundle",)), "body": ("Body file", (".md", ".txt"))}


def _purpose(purpose: str) -> tuple[str, tuple[str, ...]]:
    if purpose not in EXCHANGE:
        raise ValueError(f"unknown exchange purpose {purpose!r}")
    return EXCHANGE[purpose]


def check_components(rules: Rules, real: Path, what: str) -> None:
    """`real` is in a workspace, and no part of it below the workspace root is dot-named but .tmp."""
    check_inside(rules, real, what)
    for w in rules.workspaces:
        if real.is_relative_to(w):
            for part in real.relative_to(w).parts:
                if part.startswith(".") and part != SCRATCH:
                    raise Refused(
                        f"{what} {real} is or sits under {part!r}: the gate exchanges no dot-named path "
                        f"except {SCRATCH}/."
                    )


def check_exchange(rules: Rules, real: Path, purpose: str) -> None:
    what, suffixes = _purpose(purpose)
    check_components(rules, real, what)
    if not real.name.endswith(suffixes) or real.name in suffixes:
        raise Refused(f"{what} {real} must be named *{' or *'.join(suffixes)}.")


def open_exchange(rules: Rules, path: str | Path, purpose: str) -> tuple[int, Path]:
    """Open a workspace file the agent hands the gate; return (fd, real path). The caller closes fd.

    O_NONBLOCK means a FIFO or device is opened at once and then refused, never waited on. The
    type and link count are checked on the open file, before anything reads it.
    """
    what, _ = _purpose(purpose)
    p = require_absolute(str(path), what)
    try:
        fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as e:
        raise Refused(f"{what} {p}: {e.strerror}.") from None
    try:
        real = fd_path(fd)
        check_exchange(rules, real, purpose)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise Refused(f"{what} {p} is not a regular file.")
        if st.st_nlink != 1:
            raise Refused(f"{what} {p} has other hard links; copy it to a file of its own.")
    except BaseException:
        os.close(fd)
        raise
    return fd, real


def read_workspace_file(rules: Rules, path: str, purpose: str) -> bytes:
    what, _ = _purpose(purpose)
    fd, _ = open_exchange(rules, path, purpose)
    with os.fdopen(fd, "rb") as f:
        data = f.read(MAX_COPY + 1)
    if len(data) > MAX_COPY:
        raise Refused(f"{what} {path} is larger than {MAX_COPY} bytes.")
    return data


def copy_in(rules: Rules, path: str | Path, dst: Path, limit: int, purpose: str) -> None:
    """Stream a workspace exchange file into a new gate-owned file `dst`, refusing past `limit` bytes."""
    what, _ = _purpose(purpose)
    fd, _ = open_exchange(rules, path, purpose)
    with os.fdopen(fd, "rb") as f:
        if os.fstat(fd).st_size > limit:
            raise Refused(f"{what} {path} is larger than {limit} bytes.")
        out = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(out, "wb") as o:
                copied = 0
                while chunk := f.read(min(CHUNK, limit + 1 - copied)):
                    copied += len(chunk)
                    if copied > limit:
                        raise Refused(f"{what} {path} is larger than {limit} bytes.")
                    o.write(chunk)
        except BaseException:
            dst.unlink(missing_ok=True)
            raise


def _name(p: Path, what: str) -> str:
    name = p.name
    if name in ("", ".", "..") or p != p.parent / name:
        raise Refused(f"{what} {p} does not name a file.")
    return name


def write_out(rules: Rules, path: str | Path, src: Path, purpose: str) -> None:
    """Stream gate-owned `src` to a workspace exchange name, replacing at most a single-link regular file.

    The data goes to a fresh name in the opened parent directory and is renamed over the target,
    so a symlink at the target is replaced, never written through.
    """
    what, _ = _purpose(purpose)
    p = require_absolute(str(path), what)
    name = _name(p, what)
    with open_dir(rules, p.parent, f"{what} directory") as (dfd, real):
        check_exchange(rules, real / name, purpose)
        _require_replaceable(dfd, name, real / name, what)
        tmp = f".{name}.git-gate-{secrets.token_hex(8)}"
        try:
            out = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK, 0o644, dir_fd=dfd)
        except OSError as e:
            raise Refused(f"Cannot write {what.lower()} into {real}: {e.strerror}.") from None
        try:
            with os.fdopen(out, "wb") as o, open(src, "rb") as f:
                while chunk := f.read(CHUNK):
                    o.write(chunk)
            _require_replaceable(dfd, name, real / name, what)
            os.rename(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        except OSError as e:
            raise Refused(f"Cannot write {what.lower()} {real / name}: {e.strerror}.") from None
        finally:
            with suppress(FileNotFoundError):
                os.unlink(tmp, dir_fd=dfd)


def _require_replaceable(dir_fd: int, name: str, shown: Path, what: str) -> None:
    try:
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(st.st_mode):
        raise Refused(f"{what} {shown} exists and is not a regular file.")
    if st.st_nlink != 1:
        raise Refused(f"{what} {shown} has other hard links; the gate will not replace it.")


# --- downloads ---------------------------------------------------------------------------------


def check_new_target(rules: Rules, path: str, what: str) -> None:
    """Before a download runs: `path` must not exist, and its parent and name pass the component rule."""
    p = require_absolute(path, what)
    name = _name(p, what)
    with open_dir(rules, p.parent, f"{what} parent") as (pfd, preal):
        check_components(rules, preal / name, what)
        try:
            os.stat(name, dir_fd=pfd, follow_symlinks=False)
        except FileNotFoundError:
            return
        raise Refused(f"{what} {preal / name} already exists; name a new one, which the gate creates.")


def copy_tree_into(rules: Rules, src: Path, path: str, what: str) -> tuple[Path, list[str], list[str]]:
    """Copy a gate-owned tree into a new workspace directory the gate creates at `path`.

    The directory must not exist, so nothing already in a workspace is ever written into or
    replaced. Symlinks are skipped, and so are dot-named entries (other than .tmp), which could
    be config that runs outside the sandbox. Returns (real path, written, skipped).
    """
    p = require_absolute(path, what)
    name = _name(p, what)
    with open_dir(rules, p.parent, f"{what} parent") as (pfd, preal):
        check_components(rules, preal / name, what)
        try:
            os.mkdir(name, 0o755, dir_fd=pfd)
        except FileExistsError:
            raise Refused(f"{what} {preal / name} already exists; name a new one, which the gate creates.") from None
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=pfd)
        try:
            written, skipped = _copy_tree(src, fd)
        finally:
            os.close(fd)
    return preal / name, written, skipped


def _copy_tree(src: Path, dst_fd: int) -> tuple[list[str], list[str]]:
    written, skipped = [], []
    for entry in sorted(os.scandir(src), key=lambda e: e.name):
        if entry.is_symlink() or (entry.name.startswith(".") and entry.name != SCRATCH):
            skipped.append(entry.name)
        elif entry.is_dir():
            os.mkdir(entry.name, 0o755, dir_fd=dst_fd)
            sub = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dst_fd)
            try:
                w, s = _copy_tree(Path(entry.path), sub)
            finally:
                os.close(sub)
            written += [f"{entry.name}/{n}" for n in w]
            skipped += [f"{entry.name}/{n}" for n in s]
        elif entry.is_file():
            write_new_file_at(dst_fd, entry.name, Path(entry.path).read_bytes())
            written.append(entry.name)
    return written, skipped


def place_new_file(rules: Rules, src: Path, path: str, what: str) -> Path:
    """Copy a gate-owned file to a new workspace file at `path`; nothing there is replaced."""
    p = require_absolute(path, what)
    name = _name(p, what)
    with open_dir(rules, p.parent, f"{what} parent") as (pfd, preal):
        check_components(rules, preal / name, what)
        write_new_file_at(pfd, name, src.read_bytes())
    return preal / name


def write_new_file_at(dir_fd: int, name: str, data: bytes) -> None:
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=dir_fd)
    except FileExistsError:
        raise Refused(f"{name} already exists in the target; the gate never replaces downloaded files.") from None
    with os.fdopen(fd, "wb") as f:
        f.write(data)
