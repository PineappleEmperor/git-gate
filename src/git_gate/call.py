"""One tool call in a fresh process: `git-gate call <tool>`, arguments as JSON on stdin.

The long-running server relays every call here, through whichever release `current` points to
when the call arrives, so installing a release takes effect in open conversations without
restarting them. The answer is one JSON object on stdout: {"ok": text}, {"refused": message}
or {"error": message}.
"""

import json
import os
import sys
import time
import traceback
from pathlib import Path

from git_gate import Refused
from git_gate.config import state_dir

# The only list of tool names: the server registers exactly these, and Gate.dispatch accepts only these.
TOOLS = ("clone", "fetch", "push", "prune", "gh", "pr", "issue", "triage", "workflow_run", "label")
MAX_LOG = 1 << 20  # errors.log rolls over to errors.log.1 at this size


def log_error(text: str) -> Path | None:
    """Append to errors.log in the state directory (0600, rolled over at MAX_LOG); None if it cannot be written."""
    log = state_dir() / "errors.log"
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        try:
            if log.stat().st_size >= MAX_LOG:
                os.replace(log, log.with_name("errors.log.1"))
        except FileNotFoundError:
            pass
        fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8", errors="replace") as f:
            f.write(text)
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
