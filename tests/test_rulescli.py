"""gitgaterules: the user's own command for checking and editing rules.toml."""

import io
import shutil
import subprocess
from pathlib import Path

import pytest

from git_gate import rulescli

BIN = Path(__file__).resolve().parents[1] / "bin/gitgaterules"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_GATE_STATE_DIR", str(tmp_path / "state"))
    d = tmp_path / "cfg"
    d.mkdir()
    monkeypatch.setenv("GIT_GATE_CONFIG_DIR", str(d))
    for name in ("core", "crown", "skills"):
        (tmp_path / name / ".git").mkdir(parents=True)
    return d


def write(cfg, text: str) -> Path:
    path = cfg / "rules.toml"
    path.write_text(text.replace("{tmp}", str(cfg.parent)))
    return path


def run(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    code = rulescli.main(list(argv), stdout=out)
    return code, out.getvalue()


PINEAPPLE = """\
[groups]
pineapple = ["me/core", "me/crown"]
reads = ["up/core"]

[sessions."{tmp}/core"]
repo = "me/core"
read = ["@pineapple", "@reads"]

[sessions."{tmp}/crown"]
repo = "me/crown"
read = ["@pineapple"]

[sessions."{tmp}/skills"]
repo = "me/skills"
write = ["me/flow"]
read = ["@reads"]
"""


# --- check -----------------------------------------------------------------------------------


def test_check_ok(cfg):
    write(cfg, PINEAPPLE)
    code, out = run("check")
    assert code == 0
    assert out.strip().splitlines()[-1] == "OK: 3 sessions, 5 repos"


def test_check_lists_every_problem_with_its_line(cfg):
    write(
        cfg, PINEAPPLE + '\n[sessions."{tmp}/gone"]\nrepo = "me/gone"\n\n[sessions."{tmp}/skills2"]\nread = ["@nope"]\n'
    )
    code, out = run("check")
    assert code == 1
    errors = [line for line in out.splitlines() if line.startswith("error:")]
    assert len(errors) == 3, out
    gone = next(e for e in errors if "/gone" in e)
    assert gone.startswith("error: line 18: ") and "does not exist" in gone
    nope = next(e for e in errors if "@nope" in e)
    assert nope.startswith("error: line 22: ")
    assert "OK" not in out


def test_check_names_the_line_of_bad_toml(cfg):
    write(cfg, "[sessions\n")
    code, out = run("check")
    assert code == 1 and "not valid TOML" in out and "line 1" in out


def test_check_a_named_file(cfg, tmp_path):
    other = tmp_path / "elsewhere.toml"
    other.write_text(f'[sessions."{tmp_path}/core"]\nrepo = "me/core"\n')
    code, out = run("check", str(other))
    assert code == 0 and "OK: 1 sessions, 1 repos" in out


def test_check_warns_about_v1_and_folders_that_are_no_checkout(cfg, tmp_path):
    write(cfg, f'workspaces = ["{tmp_path}/core"]\n[repos]\n"me/core" = "write"\n')
    code, out = run("check")
    assert code == 0
    assert "warning: " in out and "v1" in out and "gitgaterules migrate" in out
    assert "OK: v1 file, 1 workspaces, 1 repos" in out
    (tmp_path / "plain").mkdir()
    write(cfg, '[sessions."{tmp}/plain"]\n')
    code, out = run("check")
    assert code == 0 and "warning: line 1: " in out and "not a git checkout" in out


def test_check_without_a_file(cfg):
    code, out = run("check")
    assert code == 1 and "No rules file" in out


# --- show ------------------------------------------------------------------------------------


def test_show_lists_each_sessions_resolved_access_once_per_repo(cfg, tmp_path):
    write(cfg, PINEAPPLE)
    code, out = run("show")
    assert code == 0
    blocks = out.strip().split("\n\n")
    assert blocks[0].splitlines() == [
        f"{tmp_path}/core",
        "  write  me/core   own repo, read @pineapple",
        "  read   me/crown  read @pineapple",
        "  read   up/core   read @reads",
    ]
    assert blocks[2].splitlines() == [
        f"{tmp_path}/skills",
        "  write  me/flow    write",
        "  write  me/skills  own repo",
        "  read   up/core    read @reads",
    ]


def test_show_refuses_a_broken_file(cfg):
    write(cfg, '[sessions."rel"]\n')
    code, out = run("show")
    assert code == 1 and "error:" in out and "absolute" in out


def test_show_v1(cfg, tmp_path):
    write(cfg, f'workspaces = ["{tmp_path}/core"]\n[repos]\n"me/core" = "write"\n"up/x" = "read"\n')
    code, out = run("show")
    assert code == 0
    assert "not scoped" in out and f"{tmp_path}/core" in out
    assert "  write  me/core" in out and "  read   up/x" in out


def test_usage(cfg):
    assert run()[0] == 2
    assert run("frobnicate")[0] == 2
    code, out = run("--help")
    assert code == 0 and "check" in out and "show" in out


# --- the launcher ------------------------------------------------------------------------------


def test_the_launcher_runs_only_from_an_installed_release(tmp_path):
    proc = subprocess.run(
        [str(BIN), "check"], env={"PATH": "/usr/bin:/bin", "GIT_GATE_ROOT": str(tmp_path)},
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert proc.returncode == 1 and "refusing to run" in proc.stderr


def test_the_launcher_runs_the_releases_python_isolated(tmp_path):
    release = tmp_path / "releases/abc"
    (release / "bin").mkdir(parents=True)
    shutil.copy(BIN, release / "bin/gitgaterules")
    python = release / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {tmp_path}/args\n')
    python.chmod(0o755)
    env = {"PATH": "/usr/bin:/bin", "GIT_GATE_ROOT": str(tmp_path)}
    subprocess.run([str(release / "bin/gitgaterules"), "check", "a b"], env=env, check=True)
    assert (tmp_path / "args").read_text().splitlines() == ["-I", "-m", "git_gate", "rules", "check", "a b"]
