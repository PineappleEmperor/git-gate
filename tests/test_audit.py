"""What Gate.call writes to audit.log for each call."""

import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
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


def rotated(env) -> list[str]:
    return sorted(p.name for p in env.state.glob("audit.log.*"))


def test_audit_log_is_private_and_rolls_over(env):
    log = env.state / "audit.log"
    log.write_text("x" * MAX_AUDIT)
    log.chmod(0o644)  # written by a release from before the roll-over
    env.gate.call("prune", ok, repo="me/app")
    [old] = rotated(env)
    assert re.fullmatch(r"audit\.log\.\d{8}T\d{6}", old)
    assert (env.state / old).read_text() == "x" * MAX_AUDIT
    assert [line["outcome"] for line in audit_lines(env)] == ["ok"]
    assert oct(log.stat().st_mode & 0o777) == "0o600"
    env.gate.call("prune", ok, repo="me/app")
    assert len(audit_lines(env)) == 2  # below the limit, it appends


def test_audit_log_rotation_never_deletes(env, monkeypatch):
    monkeypatch.setattr(call, "MAX_AUDIT", 1)
    monkeypatch.setattr(call, "_stamp", lambda: "20261025T120000")
    for branch in "abcd":
        env.gate.call("prune", ok, repo="me/app", branch=branch)
    names = ["audit.log.20261025T120000", "audit.log.20261025T120000.1", "audit.log.20261025T120000.2"]
    assert rotated(env) == names
    kept = [json.loads((env.state / name).read_text())["args"]["branch"] for name in names]
    assert kept + [audit_lines(env)[0]["args"]["branch"]] == list("abcd")


def test_concurrent_writers_lose_no_lines(env, monkeypatch):
    """Each call is its own process; the rotation is decided and done under audit.lock."""
    monkeypatch.setattr(call, "MAX_AUDIT", 300)
    monkeypatch.setattr(call, "_stamp", lambda: "20261025T120000")
    errors = []

    def write(worker: int) -> None:
        for i in range(40):
            try:
                call.audit(env.state, json.dumps({"line": f"{worker}-{i}"}))
            except OSError as e:
                errors.append(e)

    with ThreadPoolExecutor(8) as pool:
        list(pool.map(write, range(8)))
    assert errors == []
    lines = [json.loads(x)["line"] for p in env.state.glob("audit.log*") for x in p.read_text().splitlines()]
    assert sorted(lines) == sorted(f"{w}-{i}" for w in range(8) for i in range(40))
    assert oct((env.state / "audit.lock").stat().st_mode & 0o777) == "0o600"


# --- capped arguments ------------------------------------------------------------------------


def test_audit_cuts_long_argument_strings(env):
    env.gate.call("gh", ok, repo="me/app", args=["api", "q" * 1500], branch="b" * 1001)
    args = audit_lines(env)[-1]["args"]
    assert args["args"] == ["api", "q" * 1000 + "…[+500 chars]"]
    assert args["branch"] == "b" * 1000 + "…[+1 chars]"
    assert args["repo"] == "me/app"


def test_audit_scrubs_the_token_before_cutting(env):
    """A token across the cut must not leave its first characters behind."""
    with pytest.raises(Refused):
        env.gate.call("prune", raising(Refused("no")), repo="me/app", branch="x" * 990 + TOKEN + "y" * 100)
    line = (env.state / "audit.log").read_text()
    assert TOKEN[:8] not in line
    assert json.loads(line)["args"]["branch"].startswith("x" * 990 + "[token]")


def test_audit_keeps_the_head_of_each_list(env):
    have = [f"{i:040x}" for i in range(1000)]
    env.gate.call("fetch", ok, repo="me/app", bundle="/w/f.bundle", have=have)
    args = audit_lines(env)[-1]["args"]
    assert args == {"repo": "me/app", "bundle": "/w/f.bundle", "have": [*have[:20], "…[+980 more]"]}


def test_padding_cannot_hide_a_push(env):
    """Arguments that are not lists are always kept; a padded list gives way first."""
    with pytest.raises(Refused):
        env.gate.call(
            "push", env.gate.push, repo="me/app", branch="feat", bundle="/w/x.bundle",
            pad=["x" * 1000] * 10_000, more="y" * 10**6,
        )  # fmt: skip
    line = audit_lines(env)[-1]
    assert line["tool"] == "push"
    args = line["args"]
    assert (args["repo"], args["branch"], args["bundle"]) == ("me/app", "feat", "/w/x.bundle")
    assert args["more"] == "y" * 1000 + f"…[+{10**6 - 1000} chars]"
    assert args["pad"][-1] == "…[+9980 more]"


def test_padding_cannot_hide_a_gh_command(env):
    argv = ["api", "repos/me/app/pulls", *(["-f", "x" * 1000] * 2000)]
    with pytest.raises(Refused):
        env.gate.call("gh", env.gate.gh, repo="me/app", args=argv)
    args = audit_lines(env)[-1]["args"]
    assert args["repo"] == "me/app"
    assert args["args"][:3] == ["api", "repos/me/app/pulls", "-f"]
    assert args["args"][-1] == f"…[+{len(argv) - 20} more]"


def test_many_padded_lists_keep_their_heads(env):
    lists = {f"{k}_labels": ["z" * 1000] * 1000 for k in ("add", "remove")}
    lists |= {f"{k}_{w}": ["z" * 1000] * 1000 for k in ("add", "remove") for w in ("assignees", "reviewers")}
    env.gate.call("triage", ok, repo="me/app", number=7, **lists)
    args = audit_lines(env)[-1]["args"]
    assert (args["repo"], args["number"]) == ("me/app", 7)
    for name in lists:
        assert args[name][0].startswith("zzz") and args[name][-1].startswith("…[+")
    assert len(json.dumps(args)) <= gate.MAX_ARGS


def test_lists_are_emptied_before_any_other_argument_goes(env):
    lists = {f"list{i}": ["z" * 1000] * 10 for i in range(100)}
    env.gate.call("prune", ok, repo="me/app", branch="feat", **lists)
    args = audit_lines(env)[-1]["args"]
    assert (args["repo"], args["branch"]) == ("me/app", "feat")
    assert all(args[name] == ["…[+10 more]"] for name in lists)


def test_only_what_fits_is_kept_of_too_many_arguments(env):
    """The server passes only the arguments a tool declares; this is for anything else."""
    extra = {f"x{i}": "x" * 1000 for i in range(20)}
    env.gate.call("prune", ok, repo="me/app", branch="feat", **extra)
    args = audit_lines(env)[-1]["args"]
    assert (args["repo"], args["branch"]) == ("me/app", "feat")
    assert len(json.dumps(args)) <= gate.MAX_ARGS + 100
    size = len(json.dumps({"repo": "me/app", "branch": "feat"} | extra))
    assert args["…"] == f"[+{22 - len(args) + 1} more arguments, {size} chars in all]"


def test_huge_arguments_make_a_short_line(env):
    """An agent cannot fill audit.log faster than a few KiB a call, whatever it sends."""
    env.gate.call("prune", ok, repo="r" * 10**6, **{"k" * 10**6: "v" * 10**6})
    line = (env.state / "audit.log").read_text()
    assert len(line) < 10_000
    assert json.loads(line)["args"]["repo"] == "r" * 1000 + "…[+999000 chars]"


def test_arguments_the_log_cannot_hold_do_not_fail_the_call(env):
    deep = []
    for _ in range(100_000):
        deep = [deep]
    assert env.gate.call("prune", ok, repo="me/app", branch=deep) == "ok"
    assert "RecursionError" in (env.state / "errors.log").read_text()
