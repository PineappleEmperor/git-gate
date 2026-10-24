"""Running git for the gate without letting the agent's repository configure or redirect it.

git always runs with GIT_DIR pointing at a gate-owned shadow directory: its own config, HEAD,
objects/ and refs, no hooks/, info/ or commondir. The agent's objects are an fd-pinned alternate
and its refs a snapshot (see `Git.shadow`). Nothing the agent can write is read as configuration
or followed as a path, so checking .git/config cannot race a swap: the check is tamper detection
and binds the path to the repo, not the boundary.
"""

import fcntl
import hashlib
import os
import re
import shutil
import stat
import subprocess
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from git_gate import Refused
from git_gate.config import Rules
from git_gate.paths import check_inside, fd_path, open_dir

EMPTY = "empty"
ALLOWED_KEYS = re.compile(
    r"^(?:"
    r"core\.(?:repositoryformatversion|filemode|bare|logallrefupdates|ignorecase"
    r"|precomposeunicode|symlinks|autocrlf"
    r"|hookspath)"  # the gate's git never reads this config, and runs no hooks
    r"|remote\.origin\.(?:url|fetch)"
    r"|branch\..+\.(?:remote|merge|rebase|description|vscode-merge-base"
    r"|github-pr-owner-number|github-pr-base-branch)"  # PR links the VS Code PR extension writes
    r"|extensions\.objectformat"
    r"|user\.(?:name|email)"
    r"|pull\.(?:rebase|ff)"
    r"|lfs\.repositoryformatversion"
    r")$"
)
MAX_CONFIG = 64 * 1024
MAX_PACKED_REFS = 64 << 20
MAX_STDERR = 4000
WORKFLOWS = ".github/workflows"
OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
REF_PART = re.compile(r"^[^\x00-\x20\x7f~^:?*\[\\]+$")


@dataclass(frozen=True)
class Repo:
    real: Path
    gitdir: Path
    object_format: str
    head: str


@dataclass(frozen=True)
class Shadow:
    """A gate-owned GIT_DIR. Its objects/ reaches the agent's only through a pinned fd alternate."""

    path: Path
    objects_fd: int
    refs_fd: int
    snapshot: dict[str, str]


class Git:
    def __init__(self, state: Path, base_url: str, git: str = "/usr/bin/git"):
        self.state = state
        self.base_url = base_url.rstrip("/")
        self.git = git
        for d in ("shadow", "locks", "staging", "template", "home", EMPTY, "tmp"):
            (state / d).mkdir(parents=True, exist_ok=True, mode=0o700)
        self.gitconfig = state / "gitconfig"
        self._write_gitconfig()

    def url(self, repo: str) -> str:
        return f"{self.base_url}/{repo}.git"

    def env(self, token: str | None = None, git_dir: Path | None = None) -> dict[str, str]:
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(self.state / "home"),
            "LANG": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(self.gitconfig),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "",
            "SSH_ASKPASS": "",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
        }
        if token:
            env["GIT_GATE_TOKEN"] = token
        if git_dir:
            env["GIT_DIR"] = str(git_dir)
        return env

    def run(
        self,
        args: list[str],
        *,
        timeout: int,
        token: str | None = None,
        git_dir: Path | Shadow | None = None,
        check: bool = True,
        stdin: str | None = None,
    ) -> subprocess.CompletedProcess:
        if isinstance(git_dir, Shadow):
            git_dir = git_dir.path
        try:
            proc = subprocess.run(
                [self.git, *args],
                env=self.env(token, git_dir),
                cwd=self.state / EMPTY,
                input=stdin,
                stdin=None if stdin is not None else subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise Refused(f"git {args[0]} timed out after {timeout}s.") from None
        if check and proc.returncode != 0:
            raise Refused(f"git {args[0]} failed:\n{proc.stderr.strip()[-MAX_STDERR:]}")
        return proc

    def _write_gitconfig(self) -> None:
        scheme = self.base_url.split("://", 1)[0]
        settings = [
            ("core.hooksPath", "/dev/null"),
            ("core.fsmonitor", "false"),
            ("core.pager", "cat"),
            ("core.editor", "false"),
            ("core.askPass", ""),
            ("protocol.allow", "never"),
            (f"protocol.{scheme}.allow", "always"),
            ("transfer.fsckObjects", "true"),
            ("fetch.unpackLimit", "1"),
            ("transfer.unpackLimit", "1"),
            ("gc.auto", "0"),
            ("maintenance.auto", "false"),
            ("fetch.recurseSubmodules", "false"),
            ("fetch.writeCommitGraph", "false"),
            ("push.followTags", "false"),
            ("push.recurseSubmodules", "no"),
            ("push.autoSetupRemote", "false"),
            ("submodule.recurse", "false"),
            ("init.templateDir", str(self.state / "template")),
            ("advice.detachedHead", "false"),
            ("credential.helper", ""),
            (
                f"credential.{self.base_url}.helper",
                '!f() { test "$1" = get && printf \'username=x-access-token\\npassword=%s\\n\' "$GIT_GATE_TOKEN"; }; f',
            ),
        ]
        tmp = self.state / "gitconfig.new"
        tmp.unlink(missing_ok=True)
        tmp.touch(mode=0o600)
        for key, value in settings:
            subprocess.run(
                [self.git, "config", "--file", str(tmp), key, value],
                env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(self.state / "home")},
                cwd=self.state / EMPTY,
                check=True,
                capture_output=True,
            )
        tmp.replace(self.gitconfig)

    # --- agent repositories -------------------------------------------------------------

    @contextmanager
    def lock(self, key: str):
        path = self.state / "locks" / f"{hashlib.sha256(key.encode()).hexdigest()[:24]}.lock"
        with open(path, "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            yield

    def validate(self, rules: Rules, repo: str, path: str) -> Repo:
        with open_dir(rules, path, "Repository path") as (fd, real):
            try:
                st = os.stat(".git", dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                raise Refused(f"{real} has no .git directory.") from None
            if not stat.S_ISDIR(st.st_mode):
                raise Refused(f"{real}/.git must be a directory, not a gitdir: file or symlink.")
            gfd = os.open(".git", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                gitdir = fd_path(gfd)
                check_inside(rules, gitdir, "Repository .git")
                for name in ("objects", "refs"):
                    if not _is(gfd, name, stat.S_ISDIR):
                        raise Refused(f"{gitdir}/{name} must be a real directory.")
                if _exists(gfd, "packed-refs") and not _is(gfd, "packed-refs", stat.S_ISREG):
                    raise Refused(f"{gitdir}/packed-refs must be a regular file.")
                for name in ("commondir", "shallow"):
                    if _exists(gfd, name):
                        raise Refused(f"{gitdir}/{name} exists; linked and shallow repos are not supported.")
                config = _read_at(gfd, "config", MAX_CONFIG)
                head = _read_at(gfd, "HEAD", 4096).decode(errors="replace").strip()
            finally:
                os.close(gfd)
        entries = self._parse_config(config, rules)
        return Repo(real, gitdir, self._check_config(entries, repo, real), head)

    def _parse_config(self, data: bytes, rules: Rules) -> list[tuple[str, str]]:
        tmp = self.state / "tmp" / f"config-{os.getpid()}-{id(data)}"
        tmp.write_bytes(data)
        try:
            out = self.run(["config", "--file", str(tmp), "--list", "--null"], timeout=rules.git_timeout)
        except Refused as e:
            raise Refused(f".git/config does not parse: {e}") from None
        finally:
            tmp.unlink(missing_ok=True)
        entries = []
        for item in out.stdout.split("\0"):
            if item:
                key, _, value = item.partition("\n")
                entries.append((key, value))
        return entries

    def _check_config(self, entries: list[tuple[str, str]], repo: str, real: Path) -> str:
        bad = sorted({k for k, _ in entries if not ALLOWED_KEYS.match(k)})
        if bad:
            raise Refused(f"{real}/.git/config has keys outside the gate's allowlist: {', '.join(bad)}.")
        values = dict(entries)
        urls = [v for k, v in entries if k == "remote.origin.url"]
        expected = {self.url(repo).lower(), self.url(repo).lower().removesuffix(".git")}
        if len(urls) != 1 or urls[0].lower() not in expected:
            raise Refused(f"{real} remote.origin.url must be {self.url(repo)}; found {urls or 'none'}.")
        if values.get("core.bare", "false").lower() in ("true", "yes", "on", "1"):
            raise Refused(f"{real} is a bare repository.")
        fmt = values.get("extensions.objectformat", "sha1").lower()
        if fmt not in ("sha1", "sha256"):
            raise Refused(f"{real} uses unsupported object format {fmt}.")
        return fmt

    @contextmanager
    def shadow(self, rules: Rules, repo: Repo):
        """A gate-owned GIT_DIR in which git never writes, or follows a path, into the agent's .git.

        objects/ is the gate's own: the agent's object files are hardlinked into it (copied across
        filesystems), skipping info/, so an alternates file the agent plants is never read and
        swapping a directory afterwards changes nothing. refs live in a packed-refs snapshot the
        gate reads itself, skipping symlinks. New objects and refs are written back by
        `write_back`, through directory handles, never through a path the agent can redirect.
        """
        key = hashlib.sha256(str(repo.gitdir).encode()).hexdigest()[:24]
        with self.lock(key):
            fds: list[int] = []
            d = self.state / "shadow" / key
            try:
                gfd = _open_dir(repo.gitdir, fds)
                if fd_path(gfd) != repo.gitdir:
                    raise Refused(f"{repo.gitdir} moved while the gate was checking it.")
                check_inside(rules, repo.gitdir, "Repository .git")
                objects_fd = _open_dir("objects", fds, gfd)
                refs_fd = _open_dir("refs", fds, gfd)
                for name, fd in (("objects", objects_fd), ("refs", refs_fd)):
                    if fd_path(fd) != repo.gitdir / name:
                        raise Refused(f"{repo.gitdir}/{name} must be a real directory.")
                packed = _read_at(gfd, "packed-refs", MAX_PACKED_REFS) if _exists(gfd, "packed-refs") else b""
                snapshot = _parse_packed(packed) | _read_loose_refs(refs_fd, "refs/")

                shutil.rmtree(d, ignore_errors=True)
                (d / "objects/info").mkdir(parents=True, mode=0o700)
                (d / "objects/pack").mkdir()
                (d / "refs").mkdir()
                _link_objects(objects_fd, d / "objects", skip={"info"})
                version = "1" if repo.object_format == "sha256" else "0"
                config = f"[core]\n\trepositoryformatversion = {version}\n\tbare = true\n\tlogallrefupdates = false\n"
                if repo.object_format == "sha256":
                    config += "[extensions]\n\tobjectformat = sha256\n"
                (d / "config").write_text(config)
                head = repo.head if re.fullmatch(r"ref: refs/heads/[^\s]+", repo.head) else "ref: refs/heads/main"
                (d / "HEAD").write_text(head + "\n")
                (d / "packed-refs").write_text(
                    "# pack-refs with: sorted \n" + "".join(f"{snapshot[n]} {n}\n" for n in sorted(snapshot))
                )
                yield Shadow(d, objects_fd, refs_fd, snapshot)
            finally:
                for fd in fds:
                    os.close(fd)
                shutil.rmtree(d, ignore_errors=True)

    def write_back(self, sh: Shadow, prefix: str, rules: Rules) -> list[str]:
        """Copy objects git fetched into the shadow, then refs under `prefix`, into the agent's .git."""
        try:
            _merge_tree(sh.path / "objects", sh.objects_fd, skip={"info"})
        except OSError as e:
            raise Refused(f"Could not store fetched objects in the repository: {e.strerror}.") from None
        out = self.run(["for-each-ref", "--format=%(objectname) %(refname)", prefix], timeout=rules.git_timeout,
                       git_dir=sh).stdout  # fmt: skip
        changed = []
        for line in out.splitlines():
            sha, name = line.split(" ", 1)
            if sh.snapshot.get(name) != sha:
                _write_ref(sh.refs_fd, name.removeprefix("refs/"), sha)
                changed.append(name)
        return changed

    def check_branch(self, branch: str, rules: Rules) -> None:
        if not branch or branch.startswith("-") or branch == "HEAD":
            raise Refused(f"{branch!r} is not a branch name.")
        proc = self.run(["check-ref-format", f"refs/heads/{branch}"], timeout=rules.git_timeout, check=False)
        if proc.returncode != 0:
            raise Refused(f"{branch!r} is not a valid branch name.")

    def remote_heads(self, url: str, token: str, rules: Rules, git_dir: Path | None = None) -> dict[str, str]:
        out = self.run(["ls-remote", "--heads", url], timeout=rules.git_timeout, token=token, git_dir=git_dir)
        heads = {}
        for line in out.stdout.splitlines():
            sha, _, ref = line.partition("\t")
            if ref.startswith("refs/heads/"):
                heads[ref.removeprefix("refs/heads/")] = sha
        return heads

    def workflows_differ(self, a: str, b: str, git_dir: Shadow, rules: Rules) -> bool:
        out = self.run(
            ["diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-only", "-z", a, b, "--", WORKFLOWS],
            timeout=rules.git_timeout,
            git_dir=git_dir,
        )
        return bool(out.stdout.strip("\0"))

    def workflow_touches(self, shas: list[str], git_dir: Path, rules: Rules) -> list[str]:
        """Commits among shas whose own changes touch .github/workflows/ (merges: dense combined)."""
        if not shas:
            return []
        out = self.run(
            ["diff-tree", "--stdin", "-z", "-r", "--no-renames", "--name-only", "--cc", "--root"],
            timeout=rules.git_timeout,
            git_dir=git_dir,
            stdin="\n".join(shas) + "\n",
        )
        wanted = set(shas)
        current, hits = None, []
        for token in out.stdout.split("\0"):
            if token in wanted:
                current = token
            elif (token == WORKFLOWS or token.startswith(WORKFLOWS + "/")) and current and current not in hits:
                hits.append(current)
        if not hits and any(t == WORKFLOWS or t.startswith(WORKFLOWS + "/") for t in out.stdout.split("\0")):
            hits.append(shas[0])
        return hits


def _open_dir(path: str | Path, fds: list[int], dir_fd: int | None = None) -> int:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except OSError as e:
        raise Refused(f"{path} must be a real directory ({e.strerror}).") from None
    fds.append(fd)
    return fd


def _link_objects(src_fd: int, dst: Path, skip: set[str] = frozenset()) -> None:
    """Hardlink the agent's object files into a gate-owned dir, copying across filesystems.

    Symlinks are skipped, and each link is checked after it is made: a file swapped for a
    symlink between listing and linking yields a symlink, which is removed. Lock and temp
    files of a concurrent git in the sandbox come along too; git ignores them.
    """
    dst.mkdir(mode=0o700, exist_ok=True)
    for entry in os.scandir(src_fd):
        name = entry.name
        if name in skip or entry.is_symlink():
            continue
        target = dst / name
        if entry.is_dir(follow_symlinks=False):
            sub = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=src_fd)
            try:
                _link_objects(sub, target)
            finally:
                os.close(sub)
            continue
        if not entry.is_file(follow_symlinks=False):
            continue
        try:
            os.link(name, target, src_dir_fd=src_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue  # removed by a concurrent git in the sandbox
        except OSError:
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=src_fd)
            except OSError:
                continue
            with os.fdopen(fd, "rb") as f:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    continue
                with open(target, "xb") as out:
                    shutil.copyfileobj(f, out)
        if not stat.S_ISREG(target.lstat().st_mode):
            target.unlink()


def _parse_packed(data: bytes) -> dict[str, str]:
    refs = {}
    for line in data.decode(errors="replace").splitlines():
        if line.startswith(("#", "^")):
            continue
        sha, _, name = line.partition(" ")
        if OBJECT_ID.match(sha) and name.startswith("refs/") and all(REF_PART.match(p) for p in name.split("/")):
            refs[name] = sha
    return refs


def _read_loose_refs(dir_fd: int, prefix: str) -> dict[str, str]:
    """Loose refs under an open directory. Symlinks, symrefs and odd names are skipped."""
    refs = {}
    for entry in os.scandir(dir_fd):
        name = entry.name
        if entry.is_symlink() or not REF_PART.match(name) or name.endswith(".lock"):
            continue
        if entry.is_dir(follow_symlinks=False):
            sub = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
            try:
                refs |= _read_loose_refs(sub, f"{prefix}{name}/")
            finally:
                os.close(sub)
        elif entry.is_file(follow_symlinks=False):
            try:
                value = _read_at(dir_fd, name, 256).decode(errors="replace").strip()
            except Refused:
                continue
            if OBJECT_ID.match(value):
                refs[prefix + name] = value
    return refs


def _write_ref(refs_fd: int, name: str, sha: str) -> None:
    """Write refs/<name> as a loose ref, walking each component without following symlinks."""
    parts = name.split("/")
    if not all(REF_PART.match(p) for p in parts):
        raise Refused(f"Refusing odd ref name refs/{name}.")
    fd, opened = refs_fd, []
    try:
        for part in parts[:-1]:
            with suppress(FileExistsError):
                os.mkdir(part, 0o755, dir_fd=fd)
            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            opened.append(fd)
        _atomic_write(fd, parts[-1], (sha + "\n").encode())
    except OSError as e:
        raise Refused(f"Could not update refs/{name}: {e.strerror}.") from None
    finally:
        for o in opened:
            os.close(o)


def _merge_tree(src: Path, dst_fd: int, skip: set[str] = frozenset()) -> None:
    """Copy new files from a gate-owned tree into an open directory; existing names are kept.

    Pack indexes go last, so a reader never sees an .idx before its .pack.
    """
    entries = sorted(os.scandir(src), key=lambda e: (e.name.endswith(".idx"), e.name))
    for entry in entries:
        if entry.name in skip or entry.is_symlink():
            continue
        if entry.is_dir():
            with suppress(FileExistsError):
                os.mkdir(entry.name, 0o755, dir_fd=dst_fd)
            sub = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dst_fd)
            try:
                _merge_tree(Path(entry.path), sub)
            finally:
                os.close(sub)
        elif entry.is_file() and not _exists(dst_fd, entry.name):
            with open(entry.path, "rb") as f:
                _atomic_write(dst_fd, entry.name, f, mode=0o444)


def _atomic_write(dir_fd: int, name: str, data, mode: int = 0o644) -> None:
    tmp = f".{name}.git-gate-{os.getpid()}"
    with suppress(FileNotFoundError):
        os.unlink(tmp, dir_fd=dir_fd)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=dir_fd)
    with os.fdopen(fd, "wb") as out:
        if isinstance(data, bytes):
            out.write(data)
        else:
            shutil.copyfileobj(data, out)
    os.rename(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)


def _exists(dir_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def _is(dir_fd: int, name: str, test) -> bool:
    try:
        return test(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode)
    except FileNotFoundError:
        return False


def _read_at(dir_fd: int, name: str, limit: int) -> bytes:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except OSError as e:
        raise Refused(f".git/{name}: {e.strerror}.") from None
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Refused(f".git/{name} is not a regular file.")
        data = f.read(limit + 1)
    if len(data) > limit:
        raise Refused(f".git/{name} is larger than {limit} bytes.")
    return data
