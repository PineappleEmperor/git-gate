"""One tool call in a fresh process: `git-gate call <tool>`, arguments as JSON on stdin.

The long-running server relays every call here, through whichever release `current` points to
when the call arrives, so installing a release takes effect in open conversations without
restarting them. The answer is one JSON object on stdout: {"ok": text}, {"refused": message}
or {"error": message}.
"""

import json
import sys
import time
import traceback

from git_gate import Refused
from git_gate.config import state_dir

TOOLS = ("clone", "fetch", "push", "prune", "gh")


def run(tool: str, kwargs: dict, gate) -> dict:
    if tool not in TOOLS:
        return {"refused": f"git-gate has no tool {tool!r}."}
    try:
        return {"ok": gate.call(tool, getattr(gate, tool), **kwargs)}
    except Refused as e:
        return {"refused": str(e)}


def main(tool: str) -> None:
    try:
        from git_gate.gate import Gate

        result = run(tool, json.loads(sys.stdin.read() or "{}"), Gate())
    except Exception:
        log = state_dir() / "errors.log"
        with open(log, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {tool}\n{traceback.format_exc()}\n")
        result = {"error": f"git-gate hit an internal error; the traceback is in {log}."}
    sys.stdout.write(json.dumps(result))
