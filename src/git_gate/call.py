"""One tool call in a fresh process: `git-gate call <tool>`, arguments as JSON on stdin.

The long-running server relays every call here, through whichever release `current` points to
when the call arrives, so installing a release takes effect in open conversations without
restarting them. The answer is one JSON object on stdout: {"ok": text}, {"refused": message}
or {"error": message}.
"""

import fcntl
import json
import os
import sys
import time
import traceback
from pathlib import Path

from git_gate import Refused
from git_gate.config import state_dir

# The only list of tool names: the server registers exactly these, and Gate.dispatch accepts only these.
TOOLS = (
    "clone", "fetch", "push", "prune", "gh", "pr", "issue", "triage", "workflow_run", "label",
    "checks", "run", "runs", "release", "file",
)  # fmt: skip
MAX_LOG = 1 << 20  # errors.log rolls over to errors.log.1 at this size
MAX_AUDIT = 8 << 20  # audit.log is moved aside to audit.log.<time> at this size; nothing deletes those


def append(log: Path, text: str, limit: int) -> None:
    """Append `text` to `log` (0600), first moving a log of `limit` bytes or more to <log>.1. Raises OSError."""
    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        if log.stat().st_size >= limit:
            os.replace(log, log.with_name(log.name + ".1"))
    except FileNotFoundError:
        pass
    _write(log, text)


def audit(state: Path, line: str) -> None:
    """One line to <state>/audit.log, which the gate's tool calls and gitgatepush share. Raises OSError.

    A full log is moved aside under a name no file has yet, never over one: the gate keeps all of
    its audit history, so an agent that fills the log quickly cannot push old lines out of it.
    Each call is its own process, so the size is read, and the log moved and written, under
    <state>/audit.lock: two writers can neither both move it nor write to the one being moved.
    """
    log = state / "audit.log"
    fd = os.open(state / "audit.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            full = log.stat().st_size >= MAX_AUDIT
        except FileNotFoundError:
            full = False
        if full:
            base = f"{log.name}.{_stamp()}"
            name, n = base, 0
            while os.path.lexists(log.with_name(name)):
                n += 1
                name = f"{base}.{n}"
            os.rename(log, log.with_name(name))
        _write(log, line + "\n")


def _write(log: Path, text: str) -> None:
    fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8", errors="replace") as f:
        os.fchmod(fd, 0o600)  # a log written before this release may be world-readable
        f.write(text)


def _stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%S")


def log_error(text: str) -> Path | None:
    """Append to errors.log in the state directory (0600, rolled over at MAX_LOG); None if it cannot be written."""
    log = state_dir() / "errors.log"
    try:
        append(log, text, MAX_LOG)
    except OSError:
        return None
    return log


def run(tool: str, kwargs: dict, gate) -> dict:
    try:
        return {"ok": gate.dispatch(tool, kwargs)}
    except Refused as e:
        return {"refused": str(e)}


def main(tool: str) -> None:
    try:
        from git_gate.gate import Gate

        result = run(tool, json.loads(sys.stdin.read() or "{}"), Gate())
    except Exception:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        log = log_error(f"{stamp} {tool}\n{traceback.format_exc()}\n")
        where = f"the traceback is in {log}" if log else "the traceback could not be written"
        result = {"error": f"git-gate hit an internal error; {where}."}
    sys.stdout.write(json.dumps(result))
