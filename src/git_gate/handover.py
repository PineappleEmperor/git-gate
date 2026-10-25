"""Handovers: pushes the gate refused, kept for the user to run with `gitgatepush <id>`.

A record names a commit the gate already verified and pinned in its own mirror under
refs/gate/handover/<id>. `gitgatepush` runs as the user, outside the sandbox: it shows what would
go out, asks, and pushes from the mirror with the user's own SSH key and environment. The user's
git never reads the agent's repository, only the gate's mirror, with hooks off.
"""

import fcntl
import json
import os
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path

from git_gate import Refused
from git_gate.config import DEFAULT_HANDOVER_PATHS, REPO_RE, Rules, load_rules, state_dir
from git_gate.gitops import MAX_STDERR, Git
from git_gate.mirror import OID, Mirror, _matches

EXPIRES = 14 * 24 * 3600
MAX_LOG = 50
MAX_FILES = 100
MAX_FORKS = 50
ID_RE = re.compile(r"^[0-9]{1,9}$")
USAGE = "usage: gitgatepush [<id>]"
MAX_ID = 10**9 - 1
# The mirror calls made here read only git_timeout from the rules, so the user's rules.toml (which
# may be broken or absent) is not needed to run, or to purge, a handover.
RULES = Rules(workspaces=(), repos={})


@dataclass(frozen=True)
class Handover:
    id: int
    repo: str
    branch: str
    sha: str
    base: str | None
    lease: str | None
    reason: str
    created: float

    @property
    def ref(self) -> str:
        return f"refs/gate/handover/{self.id}"


class Handovers:
    """JSON records under <state>/handovers/<id>.json, ids from the flock-guarded <state>/handovers/next."""

    def __init__(self, state: Path, clock: Callable[[], float] = time.time, mirror: Mirror | None = None):
        self.dir = state / "handovers"
        self.clock = clock
        self.mirror = mirror  # when given, purging an expired record also drops its handover ref

    def create(self, repo: str, branch: str, sha: str, base: str | None, lease: str | None, reason: str) -> Handover:
        """Record a handover under the next free id.

        The id is the larger of the counter and one past the highest record file, so a lost, stale
        or corrupt counter can never reuse an id, and the counter is advanced before the record is
        written, so a crash between the two spends an id rather than reusing it.
        """
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.dir / "next", os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, "r+") as counter:
            fcntl.flock(counter, fcntl.LOCK_EX)
            try:
                stored = int(counter.read().strip())
            except ValueError:
                stored = 0
            number = max(stored if 0 < stored <= MAX_ID else 0, self._highest() + 1, 1)
            counter.seek(0)
            counter.truncate()
            counter.write(f"{number + 1}\n")
            counter.flush()
            os.fsync(counter.fileno())
            h = Handover(number, repo, branch, sha, base, lease, reason, self.clock())
            self._write(h)
            expired = self._purge()
        # After the counter lock is released: a caller may hold the mirror lock around create().
        for old in expired:
            self._drop_ref(old)
        return h

    def get(self, id: int) -> Handover:
        path = self._path(id)
        try:
            data = path.read_text()
        except FileNotFoundError:
            raise Refused(f"No open handover {id}.") from None
        except OSError as e:
            raise Refused(f"Handover {id} is unreadable: {e.strerror}.") from None
        h = _parse(data, id)
        if h is None:
            raise Refused(f"Handover {id} is unreadable: {path} is not a handover record.")
        if self._expired(h):
            raise Refused(f"No open handover {id}.")
        return h

    def open(self) -> list[Handover]:
        return [h for h in self._all() if not self._expired(h)]

    def close(self, id: int) -> None:
        self._path(id).unlink(missing_ok=True)

    def _path(self, id: int) -> Path:
        if not isinstance(id, int) or isinstance(id, bool) or id < 1:
            raise Refused(f"{id!r} is not a handover id.")
        return self.dir / f"{id}.json"

    def _expired(self, h: Handover) -> bool:
        return self.clock() - h.created > EXPIRES

    def _write(self, h: Handover) -> None:
        path = self._path(h.id)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.new")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(asdict(h), f)
        tmp.replace(path)

    def _purge(self) -> list[Handover]:
        expired = [h for h in self._all() if self._expired(h)]
        for h in expired:
            self.close(h.id)
        return expired

    def _drop_ref(self, h: Handover) -> None:
        """Unpin an expired handover's commit; a missing mirror or ref is nothing to clean up."""
        if self.mirror is not None:
            with suppress(Refused, OSError):
                self.mirror.drop(h.repo, h.ref, RULES)

    def _highest(self) -> int:
        """The highest id with a record file, readable or not."""
        ids = [int(p.stem) for p in self.dir.glob("*.json") if ID_RE.match(p.stem)]
        return max(ids, default=0)

    def _all(self) -> list[Handover]:
        found = []
        for path in self.dir.glob("*.json") if self.dir.is_dir() else ():
            if ID_RE.match(path.stem):
                try:
                    h = _parse(path.read_text(), int(path.stem))
                except OSError:
                    continue
                if h:
                    found.append(h)
        return sorted(found, key=lambda h: h.id)


def _parse(text: str, id: int) -> Handover | None:
    try:
        data = json.loads(text)
        h = Handover(**data)
    except ValueError, TypeError:
        return None
    ok = (
        h.id == id
        and isinstance(h.repo, str)
        and isinstance(h.branch, str)
        and isinstance(h.sha, str)
        and isinstance(h.reason, str)
        and all(v is None or isinstance(v, str) for v in (h.base, h.lease))
        and isinstance(h.created, int | float)
    )
    return h if ok else None


def push_command(h: Handover, mirror_dir: Path, url: str) -> list[str]:
    lease = [f"--force-with-lease=refs/heads/{h.branch}:{h.lease}"] if h.lease else []
    return [
        "git", "-c", "core.hooksPath=/dev/null", f"--git-dir={mirror_dir}", "push", *lease, url,
        f"{h.sha}:refs/heads/{h.branch}",
    ]  # fmt: skip


# --- gitgatepush ---------------------------------------------------------------------------


def main(argv: list[str], *, stdin=sys.stdin, stdout=sys.stdout, base_url: str = "https://github.com") -> int:
    def say(text: str = "", end: str = "\n", *, lines: bool = False) -> None:
        stdout.write(_clean(text, lines=lines) + end)
        stdout.flush()

    try:
        state = state_dir()
        handovers = Handovers(state)
        if not argv:
            return _list(handovers, say)
        if len(argv) != 1 or not ID_RE.match(argv[0]):
            say(USAGE)
            return 2
        return _run(handovers, int(argv[0]), Mirror(Git(state, base_url), state), stdin, say)
    except Refused as e:
        say(str(e), lines=True)
        return 1


def _list(handovers: Handovers, say) -> int:
    found = handovers.open()
    if not found:
        say("no open handovers")
    for h in found:
        say(f"{h.id}  {h.repo}  {h.branch}  {h.reason}")
    return 0


def _run(handovers: Handovers, id: int, mirror: Mirror, stdin, say) -> int:
    rules = RULES
    h = handovers.get(id)
    _check(h, mirror.git, rules)
    url = mirror.git.user_push_url(h.repo)

    with mirror.lock(h.repo):
        mirror_dir = mirror.path(h.repo)
        _require_commit(mirror, h, mirror_dir, rules)
        history = _show(mirror, h, mirror_dir, rules)
    say(f"Handover {h.id}: {h.reason}")
    say(f"Push {h.sha[:12]} to {url} as {h.branch}")
    if h.lease:
        say(f"This replaces {h.branch} (now {h.lease[:12]}), which is a force push guarded by that SHA.")
    say()
    say(history, lines=True)
    say()
    say("Run: " + shlex.join(push_command(h, mirror_dir, url)))
    say("Push it? [y/N] ", end="")
    if stdin.readline().strip().lower() not in ("y", "yes"):
        say(f"Not pushed. Handover {h.id} stays open.")
        return 1

    with mirror.lock(h.repo):
        again = handovers.get(id)  # another gitgatepush may have finished it while we waited
        if again != h:
            raise Refused(f"Handover {id} changed while waiting; run it again.")
        _check(again, mirror.git, rules)
        _require_commit(mirror, h, mirror_dir, rules)
        proc = subprocess.run(
            push_command(h, mirror_dir, url), stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False
        )
        output = (proc.stdout + proc.stderr).strip()[-MAX_STDERR:]
        if output:
            say(output, lines=True)
        if proc.returncode != 0:
            say(f"git push failed (exit {proc.returncode}). Handover {h.id} is still open.")
            return proc.returncode or 1
        handovers.close(h.id)
        try:
            mirror.drop(h.repo, h.ref, rules)
        except Refused as e:
            say(f"Pushed, but the gate's mirror could not drop {h.ref}: {e}")
            return 0
    say(f"Pushed. Handover {h.id} is closed.")
    return 0


def _check(h: Handover, git: Git, rules: Rules) -> None:
    """The record is the gate's own, but it is checked before it becomes a command line.

    Every value must match in full and print as itself, so the command shown is the argv that runs.
    """
    if not (h.repo.isprintable() and REPO_RE.fullmatch(h.repo)):
        raise Refused(f"Handover {h.id} names a bad repository.")
    if not h.branch.isprintable():
        raise Refused(f"Handover {h.id} names a branch with unprintable characters.")
    git.check_branch(h.branch, rules)
    for value in (h.sha, h.base, h.lease):
        if value is not None and not (value.isprintable() and OID.fullmatch(value)):
            raise Refused(f"Handover {h.id} holds a value that is not a commit SHA.")
    if not h.sha.strip("0"):
        raise Refused(f"Handover {h.id} holds a value that is not a commit SHA.")


def _require_commit(mirror: Mirror, h: Handover, mirror_dir: Path, rules: Rules) -> None:
    proc = mirror.git.run(
        ["cat-file", "-e", f"{h.sha}^{{commit}}"], timeout=rules.git_timeout, git_dir=mirror_dir, check=False
    )
    if proc.returncode != 0:
        raise Refused(
            f"The gate's mirror no longer holds {h.sha[:12]} for handover {h.id}; ask the agent to push again."
        )


def _handover_paths(repo: str) -> tuple[str, ...]:
    """The repo's handover patterns from rules.toml; the defaults when the rules cannot be loaded."""
    try:
        return load_rules().repo(repo).handover_paths
    except Refused, OSError:
        return DEFAULT_HANDOVER_PATHS


def _show(mirror: Mirror, h: Handover, mirror_dir: Path, rules: Rules) -> str:
    """What the push adds: the newest commits, how many are not listed, and every changed file."""

    def out(args: list[str], stdin: str | None = None) -> str:
        proc = mirror.git.run(
            [args[0], "--no-color", "--no-ext-diff", *args[1:]] if args[0] in ("log", "diff") else args,
            timeout=rules.git_timeout, git_dir=mirror_dir, check=False, stdin=stdin,
        )  # fmt: skip
        if proc.returncode != 0:
            raise Refused(f"Could not show handover {h.id}:\n{proc.stderr.strip()[-MAX_STDERR:]}")
        return proc.stdout

    # No base means a new branch: everything no mirrored branch already has.
    new = [f"{h.base}..{h.sha}"] if h.base else [h.sha, "--not", "--branches"]
    total = int(out(["rev-list", "--count", *new]).strip())
    start = h.base or _fork_point(out, h, total)

    commits = out(["log", "--oneline", "--no-decorate", f"--max-count={MAX_LOG}", *new]).rstrip()
    if total > MAX_LOG:
        commits += f"\n... and {total - MAX_LOG} more commits not shown"

    diff = ["--no-renames", start, h.sha, "--"]
    names = [f for f in out(["diff", "--name-only", "-z", *diff]).split("\0") if f]
    patterns = _handover_paths(h.repo)
    protected = [_clean(f) for f in names if _matches(f, patterns)]
    stat = out(["diff", "--stat=500,400", f"--stat-count={MAX_FILES}", *diff]).rstrip().splitlines()
    if len(names) > MAX_FILES:
        more = f" \u2026 and {len(names) - MAX_FILES} more files not listed"
        stat = [more if line.strip() == "..." else line for line in stat]
    sections = [commits]
    if protected:
        sections.append(f"Files under handover paths ({len(protected)}):\n" + "\n".join(f"  {f}" for f in protected))
    sections.append("\n".join(stat))
    return "\n\n".join(p for p in sections if p) or "(no new commits)"


def _fork_point(out, h: Handover, total: int) -> str:
    """Where a new branch leaves the mirror's branches, so only its own changes are compared.

    That is the nearest commit the new ones build on that a mirrored branch already has. The empty
    tree is used only when the mirror has no branches or the history shares nothing with them.
    """
    empty = out(["hash-object", "-t", "tree", "--stdin"], stdin="").strip()
    if not out(["for-each-ref", "--count=1", "--format=%(refname)", "refs/heads/"]).strip():
        return empty
    listed = out(["rev-list", "--boundary", h.sha, "--not", "--branches"]).splitlines()
    forks = [line[1:] for line in listed if line.startswith("-")][:MAX_FORKS]
    if not forks:
        return h.sha if total == 0 else empty  # nothing new, or history unrelated to every branch
    return min(forks, key=lambda f: int(out(["rev-list", "--count", f"{f}..{h.sha}"]).strip()))


def _clean(text: str, *, lines: bool = False) -> str:
    """Commit messages and file names come from the agent: nothing unprintable reaches the terminal.

    That covers control characters, escape sequences, bidi overrides and zero-width characters
    alike. Multi-line git output keeps its newlines; every other field is flattened to one line.
    """
    return "".join(c if c.isprintable() or (lines and c == "\n") else "?" for c in text)
