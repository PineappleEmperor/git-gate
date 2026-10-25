"""What Gate.call writes to audit.log for each call."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import TOKEN
from git_gate import Refused, call, gate
from git_gate.call import MAX_AUDIT
from git_gate.gate import HandedOver, InternalError
from git_gate.github import FromGitHub

SRC = Path(__file__).resolve().parents[1] / "src"


def audit_lines(env) -> list[dict]:
    return [json.loads(line) for line in (env.state / "audit.log").read_text().splitlines()]


def ok(rules, token, **kw):
    return "ok"


def start_time(pid: int) -> str:
    return Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[19]


# --- session and cwd -------------------------------------------------------------------------


def test_audit_names_the_calling_session_and_folder(env):
    env.gate.call("prune", ok, repo="me/app")
    line = audit_lines(env)[-1]
    assert line["session"] == f"{os.getppid()}-{start_time(os.getppid())}"
    assert line["cwd"] == os.getcwd()


def test_call_child_names_the_relay_and_its_folder(env):
    """The child the relay spawns: its parent is the relay, and it runs in the relay's folder."""
    project = env.tmp / "project"
    project.mkdir()
    (env.cfg / "disabled").touch()
    proc = subprocess.run(
        [sys.executable, "-m", "git_gate", "call", "prune"],
        input=json.dumps({"repo": "me/app", "branch": "x"}),
        capture_output=True,
        text=True,
        cwd=project,
        env=os.environ | {"PYTHONPATH": str(SRC), "GIT_GATE_CONFIG_DIR": str(env.cfg)},
        timeout=60,
    )
    assert "switched off" in json.loads(proc.stdout)["refused"]
    line = audit_lines(env)[-1]
    assert line["session"] == f"{os.getpid()}-{start_time(os.getpid())}"
    assert line["cwd"] == str(project)


def test_session_survives_odd_process_names(tmp_path):
    stat = tmp_path / str(os.getppid()) / "stat"
    stat.parent.mkdir()
    fields = ["S", *map(str, range(4, 22)), "987654", "0"]  # fields 3 to 23; 22 is the start time
    stat.write_text(f"{os.getppid()} (a) (b c) " + " ".join(fields) + "\n")
    assert gate._session(tmp_path) == f"{os.getppid()}-987654"


def test_session_falls_back_to_the_parent_pid(tmp_path):
    assert gate._session(tmp_path) == str(os.getppid())
    (tmp_path / str(os.getppid())).mkdir()
    (tmp_path / str(os.getppid()) / "stat").write_text("garbage")
    assert gate._session(tmp_path) == str(os.getppid())


def test_cwd_is_null_when_the_folder_is_gone(env, monkeypatch):
    def gone():
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(gate.os, "getcwd", gone)
    assert env.gate.call("prune", ok, repo="me/app") == "ok"
    assert audit_lines(env)[-1]["cwd"] is None


# --- reason ----------------------------------------------------------------------------------


def raising(exc: Exception):
    def tool(rules, token, **kw):
        raise exc

    return tool


@pytest.mark.parametrize(
    "exc,outcome,reason",
    [
        (Refused(f"me/app is not yours\nsecond line {TOKEN}"), "refused", "me/app is not yours"),
        (Refused(f"bad token {TOKEN}"), "refused", "bad token [token]"),
        (FromGitHub("gh exited 1:\nNot Found"), "refused", "gh exited 1:"),
        (
            HandedOver("Not pushed: it changes 1 file(s).\n\ngitgatepush 4"),
            "handover",
            "Not pushed: it changes 1 file(s).",
        ),
        (RuntimeError(f"boom {TOKEN}"), "error", "RuntimeError: boom [token]"),
    ],
)
def test_audit_says_why_a_call_was_not_ok(env, exc, outcome, reason):
    with pytest.raises((Refused, InternalError)):
        env.gate.call("prune", raising(exc), repo="me/app")
    line = audit_lines(env)[-1]
    assert (line["outcome"], line["reason"]) == (outcome, reason)


def test_audit_reason_is_null_when_ok(env):
    env.gate.call("prune", ok, repo="me/app")
    assert audit_lines(env)[-1]["reason"] is None


def test_audit_reason_is_cut_at_300_characters(env):
    with pytest.raises(Refused):
        env.gate.call("prune", raising(Refused("x" * 1000)), repo="me/app")
    assert audit_lines(env)[-1]["reason"] == "x" * 300


def test_audit_reason_for_a_real_refusal(env):
    with pytest.raises(Refused, match="allowlist") as e:
        env.gate.call("fetch", env.gate.fetch, repo="nobody/x", bundle=str(env.ws / "f.bundle"))
    assert audit_lines(env)[-1]["reason"] == str(e.value).splitlines()[0]


# --- ms --------------------------------------------------------------------------------------


def test_audit_says_how_long_the_call_took(env):
    def slow(rules, token, **kw):
        time.sleep(0.05)
        return "ok"

    env.gate.call("prune", slow, repo="me/app")
    with pytest.raises(Refused):
        env.gate.call("prune", raising(Refused("no")), repo="me/app")
    took, refused = (line["ms"] for line in audit_lines(env)[-2:])
    assert type(took) is int and 50 <= took < 5000
    assert type(refused) is int and 0 <= refused < took


# --- writing audit.log -----------------------------------------------------------------------


def test_an_unwritable_audit_log_does_not_fail_the_call(env):
    (env.state / "audit.log").mkdir()
    assert env.gate.call("prune", ok, repo="me/app") == "ok"
    with pytest.raises(Refused, match="^no$"):
        env.gate.call("prune", raising(Refused("no")), repo="me/app")
    assert "audit.log" in (env.state / "errors.log").read_text()


def test_audit_log_is_private_and_rolls_over(env):
    log = env.state / "audit.log"
    log.write_text("x" * MAX_AUDIT)
    log.chmod(0o644)  # written by a release from before the roll-over
    env.gate.call("prune", ok, repo="me/app")
    assert (env.state / "audit.log.1").read_text() == "x" * MAX_AUDIT
    assert [line["outcome"] for line in audit_lines(env)] == ["ok"]
    assert oct(log.stat().st_mode & 0o777) == "0o600"
    env.gate.call("prune", ok, repo="me/app")
    assert len(audit_lines(env)) == 2  # below the limit, it appends


def test_audit_log_keeps_one_old_file(env, monkeypatch):
    monkeypatch.setattr(call, "MAX_AUDIT", 1)
    for _ in range(3):
        env.gate.call("prune", ok, repo="me/app")
    assert sorted(p.name for p in env.state.glob("audit.log*")) == ["audit.log", "audit.log.1"]
    assert len(audit_lines(env)) == 1
