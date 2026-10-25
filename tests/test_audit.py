"""What Gate.call writes to audit.log for each call."""

import json
import os
import subprocess
import sys
from pathlib import Path

from git_gate import gate

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
