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
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from git_gate import Refused
from git_gate.config import REPO_RE, Rules, state_dir
from git_gate.gitops import MAX_STDERR, Git
from git_gate.mirror import OID, Mirror

EXPIRES = 14 * 24 * 3600
MAX_LOG = 50
ID_RE = re.compile(r"^[0-9]{1,9}$")
UNSAFE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
USAGE = "usage: gitgatepush [<id>]"


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

    def __init__(self, state: Path, clock: Callable[[], float] = time.time):
        self.dir = state / "handovers"
        self.clock = clock

    def create(self, repo: str, branch: str, sha: str, base: str | None, lease: str | None, reason: str) -> Handover:
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.dir / "next", os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, "r+") as counter:
            fcntl.flock(counter, fcntl.LOCK_EX)
            number = int(counter.read().strip() or 1)
            h = Handover(number, repo, branch, sha, base, lease, reason, self.clock())
            self._write(h)
            counter.seek(0)
            counter.truncate()
            counter.write(f"{number + 1}\n")
            counter.flush()
            self._purge()
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

    def _purge(self) -> None:
        for h in self._all():
            if self._expired(h):
                self.close(h.id)

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
    def say(text: str = "", end: str = "\n") -> None:
        stdout.write(_clean(text) + end)
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
        say(str(e))
        return 1


def _list(handovers: Handovers, say) -> int:
    found = handovers.open()
    if not found:
        say("no open handovers")
    for h in found:
        say(f"{h.id}  {h.repo}  {h.branch}  {h.reason}")
    return 0


def _run(handovers: Handovers, id: int, mirror: Mirror, stdin, say) -> int:
    # Only git_timeout is read from rules by the mirror calls used here, so the user's rules.toml
    # (which may be broken or absent) is not needed to run a handover.
    rules = Rules(workspaces=(), repos={})
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
    say(history)
    say()
    say("Run: " + " ".join(push_command(h, mirror_dir, url)))
    say("Push it? [y/N] ", end="")
    if stdin.readline().strip().lower() not in ("y", "yes"):
        say(f"Not pushed. Handover {h.id} stays open.")
        return 1

    with mirror.lock(h.repo):
        h = handovers.get(id)  # another gitgatepush may have finished it while we waited
        _require_commit(mirror, h, mirror_dir, rules)
        proc = subprocess.run(
            push_command(h, mirror_dir, url), stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False
        )
        output = (proc.stdout + proc.stderr).strip()[-MAX_STDERR:]
        if output:
            say(output)
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
    """The record is the gate's own, but it is checked before it becomes a command line."""
    if not REPO_RE.match(h.repo):
        raise Refused(f"Handover {h.id} names a bad repository.")
    git.check_branch(h.branch, rules)
    for value in (h.sha, h.base, h.lease):
        if value is not None and not OID.match(value):
            raise Refused(f"Handover {h.id} holds a value that is not a commit SHA.")


def _require_commit(mirror: Mirror, h: Handover, mirror_dir: Path, rules: Rules) -> None:
    proc = mirror.git.run(
        ["cat-file", "-e", f"{h.sha}^{{commit}}"], timeout=rules.git_timeout, git_dir=mirror_dir, check=False
    )
    if proc.returncode != 0:
        raise Refused(
            f"The gate's mirror no longer holds {h.sha[:12]} for handover {h.id}; ask the agent to push again."
        )


def _show(mirror: Mirror, h: Handover, mirror_dir: Path, rules: Rules) -> str:
    """The commits and file changes the push would add, from the mirror."""
    if h.base:
        steps = [["log", "--oneline", "--no-decorate", f"{h.base}..{h.sha}"], ["diff", "--stat", h.base, h.sha]]
    else:
        # A new branch: everything no mirrored branch already has.
        log = ["log", "--oneline", "--no-decorate", "--stat", f"--max-count={MAX_LOG}", h.sha, "--not", "--branches"]
        steps = [log]
    parts = []
    for step in steps:
        proc = mirror.git.run(
            [step[0], "--no-color", "--no-ext-diff", *step[1:]], timeout=rules.git_timeout, git_dir=mirror_dir,
            check=False,
        )  # fmt: skip
        if proc.returncode != 0:
            raise Refused(f"Could not show handover {h.id}:\n{proc.stderr.strip()[-MAX_STDERR:]}")
        parts.append(proc.stdout.rstrip())
    return "\n\n".join(p for p in parts if p) or "(no new commits)"


def _clean(text: str) -> str:
    """Commit messages and file names come from the agent: no escape sequences reach the terminal."""
    return UNSAFE.sub("?", text)
