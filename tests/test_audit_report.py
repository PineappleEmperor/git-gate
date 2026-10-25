"""scripts/audit_report.py: the user's summary of audit.log."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/audit_report.py"
spec = importlib.util.spec_from_file_location("audit_report", SCRIPT)
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


def gate_line(day="2026-10-20", tool="push", repo="me/app", outcome="ok", **extra) -> str:
    line = {"time": f"{day}T10:00:00+0100", "tool": tool, "args": {"repo": repo, "branch": "x"}, "outcome": outcome}
    return json.dumps(line | extra)


def push_line(day="2026-10-20", repo="me/app", outcome="pushed") -> str:
    line = {"time": f"{day}T11:00:00+0100", "tool": "gitgatepush", "id": 3, "repo": repo, "branch": "x"}
    return json.dumps(line | {"sha": "a" * 40, "outcome": outcome})


def write(path: Path, *lines: str) -> Path:
    path.write_text("".join(line + "\n" for line in lines))
    return path


def run(capsys, *argv: str) -> tuple[int, str]:
    code = report.main(list(argv))
    return code, capsys.readouterr().out


def section(out: str, title: str) -> list[str]:
    """The lines of one section: from its title to the next blank line."""
    lines = out.splitlines()
    start = lines.index(title)
    end = lines.index("", start) if "" in lines[start:] else len(lines)
    return lines[start + 1 : end]


def row(lines: list[str], *words: str) -> list[str]:
    """The one row whose leading cells are `words`, split on whitespace."""
    found = [line.split() for line in lines if line.split()[: len(words)] == list(words)]
    assert len(found) == 1, (words, lines)
    return found[0]


@pytest.fixture
def log(tmp_path) -> Path:
    write(
        tmp_path / "audit.log.1",
        gate_line("2026-10-01", outcome="refused", reason="me/app is read-only", session="1-1", cwd="/w/old"),
        "not json at all",
    )
    return write(
        tmp_path / "audit.log",
        gate_line(),  # an old line: no session, cwd, reason or ms
        gate_line(outcome="refused", reason="main is protected", session="2-5", cwd="/w/app", ms=40),
        gate_line(outcome="refused", reason="main is protected", session="2-5", cwd="/w/app", ms=41),
        gate_line(outcome="handover", reason="Not pushed: x", session="3-7", cwd="/w/app", ms=900),
        gate_line(tool="gh", repo="other/lib", reason=None, session="3-7", cwd="/w/app", ms=12),
        gate_line(tool="fetch", outcome="error", reason="OSError: disk full", session="3-7", cwd="/w/app"),
        push_line(),
        push_line(outcome="failed"),
        "[1, 2]",
        '{"time": 5}',
        "",
    )


def test_calls_per_folder(capsys, log):
    code, out = run(capsys, str(log))
    assert code == 0
    lines = section(out, "Calls per session folder")
    # folder, sessions, calls, ok, refused, handover, error
    assert row(lines, "/w/app") == ["/w/app", "2", "5", "1", "2", "1", "1"]
    assert row(lines, "/w/old") == ["/w/old", "1", "1", "0", "1", "0", "0"]
    assert row(lines, "(not", "recorded)")[2:] == ["0", "1", "1", "0", "0", "0"]


def test_calls_per_repo_and_tool(capsys, log):
    lines = section(run(capsys, str(log))[1], "Calls per repo and tool")
    assert row(lines, "me/app", "push") == ["me/app", "push", "5", "1", "3", "1", "0"]
    assert row(lines, "other/lib", "gh") == ["other/lib", "gh", "1", "1", "0", "0", "0"]
    assert row(lines, "me/app", "fetch") == ["me/app", "fetch", "1", "0", "0", "0", "1"]


def test_refusal_reasons_most_common_first(capsys, log):
    lines = section(run(capsys, str(log))[1], "Most common refusal reasons")
    body = [line.split(None, 3) for line in lines[1:]]
    assert body[0] == ["2", "me/app", "push", "main is protected"]
    assert ["1", "me/app", "fetch", "OSError: disk full"] in body
    assert ["1", "me/app", "push", "Not pushed: x"] in body
    assert len(body) == 4


def test_handover_outcomes(capsys, log):
    lines = section(run(capsys, str(log))[1], "Handovers")
    # repo, handed over, pushed, failed
    assert row(lines, "me/app") == ["me/app", "1", "1", "1"]


def test_counts_lines_it_skips(capsys, log):
    out = run(capsys, str(log))[1]
    assert "3 malformed line(s) skipped" in out
    assert "2026-10-01 to 2026-10-20" in out


def test_since(capsys, log):
    code, out = run(capsys, "--since", "2026-10-02", str(log))
    assert code == 0
    assert "/w/old" not in out
    assert row(section(out, "Calls per repo and tool"), "me/app", "push")[2] == "4"


def test_bad_since(capsys, log):
    with pytest.raises(SystemExit):
        report.main(["--since", "yesterday", str(log)])


def test_no_log(capsys, tmp_path):
    code, out = run(capsys, str(tmp_path / "audit.log"))
    assert code == 1
    assert "no audit log" in out


def test_reads_the_old_file_alone(capsys, tmp_path):
    write(tmp_path / "audit.log.1", gate_line(session="1-1", cwd="/w/x"))
    code, out = run(capsys, str(tmp_path / "audit.log"))
    assert code == 0
    assert row(section(out, "Calls per session folder"), "/w/x")[2] == "1"


def test_default_path_follows_the_state_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("GIT_GATE_STATE_DIR", str(tmp_path))
    assert report.default_log() == tmp_path / "audit.log"
    monkeypatch.delenv("GIT_GATE_STATE_DIR")
    assert report.default_log() == Path.home() / ".local/state/git-gate/audit.log"


def test_runs_as_a_script(log):
    proc = subprocess.run([sys.executable, str(SCRIPT), str(log)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "Calls per repo and tool" in proc.stdout
