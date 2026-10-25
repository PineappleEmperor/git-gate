"""gitgaterules: the user's own command for checking and editing rules.toml."""

import io
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from git_gate import Refused, rulescli
from git_gate.config import load_rules

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
    assert code == 0 and "check" in out and "show" in out and "grant" in out


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


# --- edits -------------------------------------------------------------------------------------


def backups(cfg) -> list[Path]:
    return sorted(cfg.glob("rules.toml.bak-*"))


def mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_grant_writes_a_canonical_file_with_a_backup_and_a_diff(cfg, tmp_path):
    old = write(cfg, "# my comment\n" + PINEAPPLE).read_text()
    code, out = run("grant", f"{tmp_path}/crown", "read", "up/core")
    assert code == 0, out
    assert "+read = [" in out or '+read = ["@pineapple", "up/core"]' in out
    assert f"Backup of the old file: {backups(cfg)[0]}" in out
    [bak] = backups(cfg)
    assert re.fullmatch(r"rules\.toml\.bak-\d{8}T\d{6}", bak.name)
    assert bak.read_text() == old and mode(bak) == 0o600
    text = (cfg / "rules.toml").read_text()
    assert mode(cfg / "rules.toml") == 0o600
    assert text.startswith("# Managed by gitgaterules") and "my comment" not in text
    rules = load_rules(cfg).scoped(f"{tmp_path}/crown")
    assert rules.access("up/core") == "read"
    assert [p.name for p in cfg.iterdir() if p.name.startswith(".")] == []  # no temp file left


def test_the_written_file_is_canonical(cfg, tmp_path):
    write(cfg, PINEAPPLE)
    run("grant", f"{tmp_path}/skills", "write", "Me/Another")
    text = (cfg / "rules.toml").read_text()
    assert rulescli.render(tomllib.loads(text)) == text
    assert text.index("[groups]") < text.index("[sessions.") < text.index(f'"{tmp_path}/skills"')
    assert f'[sessions."{tmp_path}/core"]\nrepo = "me/core"\nread = ["@pineapple", "@reads"]\n' in text
    assert 'write = ["Me/Another", "me/flow"]' in text


def test_grant_write_moves_a_read_grant(cfg, tmp_path):
    write(cfg, '[sessions."{tmp}/core"]\nread = ["me/x"]\n')
    code, out = run("grant", f"{tmp_path}/core", "write", "me/x")
    assert code == 0 and "moved from read to write" in out
    assert load_rules(cfg).scoped(f"{tmp_path}/core").access("me/x") == "write"


def test_grant_accepts_a_relative_folder(cfg, tmp_path, monkeypatch):
    write(cfg, PINEAPPLE)
    monkeypatch.chdir(tmp_path / "core")
    assert run("grant", ".", "write", "me/x")[0] == 0
    assert load_rules(cfg).scoped(f"{tmp_path}/core").access("me/x") == "write"


@pytest.mark.parametrize(
    ("argv", "words"),
    [
        (("grant", "{tmp}/nowhere", "read", "me/x"), ["No session", "add-session"]),
        (("grant", "{tmp}/core", "read", "@nope"), ["@nope", "Not written"]),
        (("grant", "{tmp}/core", "read", "not-a-repo"), ["not-a-repo", "owner/name"]),
        (("grant", "{tmp}/core", "admin", "me/x"), ["usage"]),
        (("revoke", "{tmp}/core", "me/never"), ["me/never", "not granted"]),
        (("add-session", "{tmp}/core"), ["already"]),
        (("add-session", "{tmp}/missing", "--repo", "me/x"), ["does not exist", "Not written"]),
        (("remove-session", "{tmp}/nowhere"), ["No session"]),
        (("group", "reads", "remove", "me/never"), ["me/never", "not in"]),
        (("group", "reads", "add", "@pineapple"), []),
        (("group", "pineapple", "add", "@pineapple"), ["includes itself", "Not written"]),
    ],
)
def test_a_bad_edit_writes_nothing(cfg, tmp_path, argv, words):
    before = write(cfg, PINEAPPLE).read_text()
    code, out = run(*(a.replace("{tmp}", str(tmp_path)) for a in argv))
    if not words:  # a group may hold a group
        assert code == 0, out
        return
    assert code in (1, 2), out
    for word in words:
        assert word in out
    assert (cfg / "rules.toml").read_text() == before and backups(cfg) == []


def test_revoke(cfg, tmp_path):
    write(cfg, PINEAPPLE)
    code, out = run("revoke", f"{tmp_path}/skills", "me/flow")
    assert code == 0
    with pytest.raises(Refused):
        load_rules(cfg).scoped(f"{tmp_path}/skills").access("me/flow")
    code, out = run("revoke", f"{tmp_path}/core", "me/core")
    assert code == 0 and "still reached through read @pineapple" in out
    assert load_rules(cfg).scoped(f"{tmp_path}/core").access("me/core") == "read"
    assert run("revoke", f"{tmp_path}/core", "@reads")[0] == 0
    assert "up/core" not in load_rules(cfg).scoped(f"{tmp_path}/core").repos


def test_add_and_remove_a_session(cfg, tmp_path):
    (tmp_path / "new").mkdir()
    code, out = run("add-session", f"{tmp_path}/new/../new", "--repo", "me/new")
    assert code == 0, out
    assert f'[sessions."{tmp_path}/new"]\nrepo = "me/new"\n' in (cfg / "rules.toml").read_text().replace("\r", "")
    assert run("remove-session", f"{tmp_path}/new")[0] == 0
    assert "/new" not in (cfg / "rules.toml").read_text()


def test_the_first_edit_creates_the_file(cfg, tmp_path):
    code, out = run("add-session", f"{tmp_path}/core", "--repo", "me/core")
    assert code == 0, out
    assert mode(cfg / "rules.toml") == 0o600 and backups(cfg) == []
    assert load_rules(cfg).scoped(f"{tmp_path}/core").access("me/core") == "write"


def test_removing_the_last_session_keeps_the_file_v2(cfg, tmp_path):
    write(cfg, '[sessions."{tmp}/core"]\nrepo = "me/core"\n')
    assert run("remove-session", f"{tmp_path}/core")[0] == 0
    assert load_rules(cfg).version == 2


def test_remove_session_mends_a_file_whose_folder_is_gone(cfg, tmp_path):
    write(cfg, PINEAPPLE + '[sessions."{tmp}/gone"]\nrepo = "me/gone"\n')
    with pytest.raises(Refused, match="does not exist"):
        load_rules(cfg)
    assert run("remove-session", f"{tmp_path}/gone")[0] == 0
    assert load_rules(cfg).version == 2


def test_group_add_and_remove(cfg, tmp_path):
    write(cfg, PINEAPPLE)
    assert run("group", "pineapple", "add", "me/frond")[0] == 0
    assert load_rules(cfg).scoped(f"{tmp_path}/crown").access("me/frond") == "read"
    assert run("group", "fresh", "add", "me/y")[0] == 0
    assert 'fresh = ["me/y"]' in (cfg / "rules.toml").read_text()
    assert run("group", "pineapple", "remove", "me/frond")[0] == 0
    with pytest.raises(Refused):
        load_rules(cfg).scoped(f"{tmp_path}/crown").access("me/frond")


def test_no_change_writes_nothing(cfg, tmp_path):
    write(cfg, PINEAPPLE)
    run("grant", f"{tmp_path}/core", "read", "up/x")
    before = (cfg / "rules.toml").read_text()
    n = len(backups(cfg))
    code, out = run("grant", f"{tmp_path}/core", "read", "up/x")
    assert code == 0 and "No change" in out
    assert (cfg / "rules.toml").read_text() == before and len(backups(cfg)) == n


def test_edits_refuse_a_v1_file(cfg, tmp_path):
    before = write(cfg, f'workspaces = ["{tmp_path}/core"]\n[repos]\n"me/core" = "write"\n').read_text()
    code, out = run("grant", f"{tmp_path}/core", "read", "me/x")
    assert code == 1 and "gitgaterules migrate" in out
    assert (cfg / "rules.toml").read_text() == before


def test_a_symlinked_rules_file_is_written_through(cfg, tmp_path):
    real = tmp_path / "dotfiles/rules.toml"
    real.parent.mkdir()
    real.write_text(PINEAPPLE.replace("{tmp}", str(tmp_path)))
    (cfg / "rules.toml").symlink_to(real)
    assert run("grant", f"{tmp_path}/core", "read", "up/x")[0] == 0
    assert (cfg / "rules.toml").is_symlink() and "up/x" in real.read_text()
    assert len(list(real.parent.glob("rules.toml.bak-*"))) == 1
