import json
import os
import subprocess
import sys
from pathlib import Path

import anyio
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from git_gate import Refused
from git_gate.call import run
from git_gate.server import Relay, build

SRC = Path(__file__).resolve().parents[1] / "src"


def _release(root: Path, name: str, answer: dict, relay_aware: bool = True) -> Path:
    """A fake installed release whose launcher records its call and prints `answer`."""
    rel = root / "releases" / name
    (rel / "bin").mkdir(parents=True)
    (rel / "src/git_gate").mkdir(parents=True)
    if relay_aware:
        (rel / "src/git_gate/call.py").write_text("")
    script = rel / "bin/git-gate"
    script.write_text(f"#!/bin/sh\necho \"$@\" > {rel}/argv\ncat > {rel}/stdin\necho '{json.dumps(answer)}'\n")
    script.chmod(0o755)
    return rel


def _point(root: Path, name: str) -> None:
    tmp = root / "current.new"
    tmp.unlink(missing_ok=True)
    tmp.symlink_to(f"releases/{name}")
    tmp.replace(root / "current")


def test_relay_follows_current_between_calls(tmp_path):
    one = _release(tmp_path, "one", {"ok": "from one"})
    _release(tmp_path, "two", {"ok": "from two"})
    _point(tmp_path, "one")
    relay = Relay(tmp_path)
    assert relay("fetch", {"repo": "me/app", "path": "/w"}) == "from one"
    assert (one / "argv").read_text().split() == ["call", "fetch"]
    assert json.loads((one / "stdin").read_text()) == {"repo": "me/app", "path": "/w"}
    _point(tmp_path, "two")
    assert relay("fetch", {}) == "from two"


@pytest.mark.parametrize(
    "answer,message",
    [
        ({"refused": "not on the allowlist"}, "not on the allowlist"),
        ({"error": "internal error; see errors.log"}, "errors.log"),
        ({}, "returned nothing"),
    ],
)
def test_relay_turns_answers_into_refusals(tmp_path, answer, message):
    _release(tmp_path, "r", answer)
    _point(tmp_path, "r")
    with pytest.raises(Refused, match=message):
        Relay(tmp_path)("gh", {})


def test_relay_reports_a_release_that_gives_no_answer(tmp_path):
    rel = _release(tmp_path, "r", {})
    (rel / "bin/git-gate").write_text("#!/bin/sh\necho not json\n")
    _point(tmp_path, "r")
    with pytest.raises(Refused, match="gave no answer"):
        Relay(tmp_path)("gh", {})


def test_relay_runs_in_process_for_a_release_from_before_the_relay(tmp_path, env):
    _release(tmp_path, "old", {"ok": "must not be used"}, relay_aware=False)
    _point(tmp_path, "old")
    relay = Relay(tmp_path, fallback=lambda: env.gate)
    with pytest.raises(Refused, match="allowlist"):
        relay("fetch", {"repo": "nobody/x", "path": str(env.ws)})


def test_server_uses_the_relay(tmp_path):
    _release(tmp_path, "r", {"ok": "relayed"})
    _point(tmp_path, "r")
    server = build(relay=Relay(tmp_path))
    assert "relayed" in str(anyio.run(server.call_tool, "prune", {"repo": "me/app", "branch": "x"}))
    _release(tmp_path, "s", {"refused": "nope"})
    _point(tmp_path, "s")
    with pytest.raises(ToolError, match="nope"):
        anyio.run(server.call_tool, "prune", {"repo": "me/app", "branch": "x"})


def test_dev_checkout_is_not_relayed():
    assert Relay.for_installed() is None


def test_call_run(env):
    assert "reset --hard" in run("clone", {"repo": "me/app", "path": str(env.ws / "c")}, env.gate)["ok"]
    assert "allowlist" in run("fetch", {"repo": "nobody/x", "path": str(env.ws)}, env.gate)["refused"]
    assert "no tool" in run("rm", {}, env.gate)["refused"]


def test_call_subprocess_end_to_end(env):
    """The real child: `python -m git_gate call <tool>` with JSON in, JSON out."""
    proc = subprocess.run(
        [sys.executable, "-m", "git_gate", "call", "fetch"],
        input=json.dumps({"repo": "nobody/x", "path": str(env.ws)}),
        capture_output=True,
        text=True,
        env=os.environ | {"PYTHONPATH": str(SRC), "GIT_GATE_CONFIG_DIR": str(env.cfg)},
        timeout=60,
    )
    assert "allowlist" in json.loads(proc.stdout)["refused"]
