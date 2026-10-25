import io
import json
import os
import subprocess
import sys
from pathlib import Path

import anyio
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from git_gate import Refused, server
from git_gate.call import main as call_main
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


@pytest.mark.parametrize("key", ["ok", "refused", "error"])
def test_relay_answers_are_valid_utf8(tmp_path, key):
    """A child's JSON may escape a lone surrogate; json.loads brings it back, so the relay must clean it."""
    _release(tmp_path, "r", {key: ".githooks/\udcff gitgatepush 1"})
    _point(tmp_path, "r")
    relay = Relay(tmp_path)
    if key == "ok":
        text = relay("push", {})
    else:
        with pytest.raises(Refused) as e:
            relay("push", {})
        text = str(e.value)
    text.encode("utf-8")
    assert ".githooks/? gitgatepush 1" in text


def test_relay_reports_a_release_that_gives_no_answer(tmp_path):
    rel = _release(tmp_path, "r", {})
    (rel / "bin/git-gate").write_text("#!/bin/sh\necho not json\n")
    _point(tmp_path, "r")
    with pytest.raises(Refused, match="gave no answer"):
        Relay(tmp_path)("gh", {})


def test_relay_logs_a_child_that_dies_before_answering(tmp_path, monkeypatch):
    state = tmp_path / "missing" / "state"
    monkeypatch.setenv("GIT_GATE_STATE_DIR", str(state))
    rel = _release(tmp_path, "r", {})
    (rel / "bin/git-gate").write_text(
        "#!/bin/sh\necho 'Traceback (most recent call last):' >&2\necho 'ImportError: boom' >&2\nexit 3\n"
    )
    _point(tmp_path, "r")
    with pytest.raises(Refused, match="errors.log") as caught:
        Relay(tmp_path)("gh", {})
    assert str(state / "errors.log") in str(caught.value)
    log = (state / "errors.log").read_text()
    assert "ImportError: boom" in log
    assert "3" in log
    assert " gh" in log


@pytest.mark.parametrize("stdout", ["null", "[]", '"x"', '{"ok": 5}', '{"refused": 5}', '{"error": null}'])
def test_relay_rejects_answers_that_are_not_well_formed(tmp_path, monkeypatch, stdout):
    monkeypatch.setenv("GIT_GATE_STATE_DIR", str(tmp_path / "state"))
    rel = _release(tmp_path, "r", {})
    (rel / "bin/git-gate").write_text(f"#!/bin/sh\necho '{stdout}'\n")
    _point(tmp_path, "r")
    with pytest.raises(Refused, match="gave no answer"):
        Relay(tmp_path)("gh", {})


def test_call_main_survives_a_missing_state_dir(tmp_path, monkeypatch, capsys):
    state = tmp_path / "missing" / "state"
    monkeypatch.setenv("GIT_GATE_STATE_DIR", str(state))

    def broken():
        raise RuntimeError("no gate")

    monkeypatch.setattr("git_gate.gate.Gate", broken)
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    call_main("fetch")
    answer = json.loads(capsys.readouterr().out)
    assert "errors.log" in answer["error"]
    assert "RuntimeError: no gate" in (state / "errors.log").read_text()


def test_relay_runs_in_process_for_a_release_from_before_the_relay(tmp_path, env):
    _release(tmp_path, "old", {"ok": "must not be used"}, relay_aware=False)
    _point(tmp_path, "old")
    relay = Relay(tmp_path, fallback=lambda: env.gate)
    with pytest.raises(Refused, match="allowlist"):
        relay("fetch", {"repo": "nobody/x", "bundle": str(env.ws / "f.bundle")})


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


def test_relay_timeout_and_oserror_messages_differ(tmp_path, monkeypatch):
    rel = _release(tmp_path, "slowrelease", {"ok": "never"})
    _point(tmp_path, "slowrelease")
    (rel / "bin/git-gate").write_text("#!/bin/sh\nsleep 5\n")
    monkeypatch.setattr(server, "RELAY_TIMEOUT", 0.2)
    with pytest.raises(Refused) as slow:
        Relay(tmp_path)("gh", {})
    (rel / "bin/git-gate").unlink()
    with pytest.raises(Refused) as missing:
        Relay(tmp_path)("gh", {})
    assert "timed out" in str(slow.value) and "slowrelease" in str(slow.value)
    assert "could not start" in str(missing.value) and "slowrelease" in str(missing.value)
    assert str(slow.value) != str(missing.value)


def test_relay_decodes_a_childs_non_utf8_output_with_replacement(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_GATE_STATE_DIR", str(tmp_path / "state"))
    rel = _release(tmp_path, "r", {})
    (rel / "bin/git-gate").write_text("#!/bin/sh\nprintf 'bad \\377 byte' >&2\nexit 1\n")
    _point(tmp_path, "r")
    with pytest.raises(Refused, match="gave no answer"):
        Relay(tmp_path)("gh", {})
    assert "bad \ufffd byte" in (tmp_path / "state/errors.log").read_text()


def test_relay_decodes_a_non_utf8_answer_with_replacement(tmp_path):
    rel = _release(tmp_path, "r", {})
    (rel / "bin/git-gate").write_text('#!/bin/sh\nprintf \'{"ok": "name \\377 here"}\'\n')
    _point(tmp_path, "r")
    assert Relay(tmp_path)("gh", {}) == "name \ufffd here"


def test_errors_log_is_private_and_rolls_over(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("GIT_GATE_STATE_DIR", str(state))
    rel = _release(tmp_path, "r", {})
    (rel / "bin/git-gate").write_text("#!/bin/sh\necho first >&2\nexit 3\n")
    _point(tmp_path, "r")
    with pytest.raises(Refused):
        Relay(tmp_path)("gh", {})
    log = state / "errors.log"
    assert oct(log.stat().st_mode & 0o777) == "0o600"
    log.write_text("x" * (1 << 20))
    with pytest.raises(Refused):
        Relay(tmp_path)("gh", {})
    assert (state / "errors.log.1").read_text() == "x" * (1 << 20)
    assert "first" in log.read_text() and log.stat().st_size < 1 << 20
    assert oct(log.stat().st_mode & 0o777) == "0o600"


def test_call_main_writes_a_private_log(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("GIT_GATE_STATE_DIR", str(state))
    monkeypatch.setattr("git_gate.gate.Gate", lambda: 1 / 0)
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    call_main("fetch")
    assert oct((state / "errors.log").stat().st_mode & 0o777) == "0o600"


def test_call_run(env):
    assert "reset --hard" in run("clone", {"repo": "me/app", "path": str(env.ws / "c")}, env.gate)["ok"]
    assert "allowlist" in run("fetch", {"repo": "nobody/x", "bundle": str(env.ws / "f.bundle")}, env.gate)["refused"]
    assert "no tool" in run("rm", {}, env.gate)["refused"]


def test_call_subprocess_end_to_end(env):
    """The real child: `python -m git_gate call <tool>` with JSON in, JSON out."""
    proc = subprocess.run(
        [sys.executable, "-m", "git_gate", "call", "fetch"],
        input=json.dumps({"repo": "nobody/x", "bundle": str(env.ws / "f.bundle")}),
        capture_output=True,
        text=True,
        env=os.environ | {"PYTHONPATH": str(SRC), "GIT_GATE_CONFIG_DIR": str(env.cfg)},
        timeout=60,
    )
    assert "allowlist" in json.loads(proc.stdout)["refused"]
