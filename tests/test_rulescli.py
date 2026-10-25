"""gitgaterules: the user's own command for checking and editing rules.toml."""

import io
import json
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from conftest import git
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


class Terminal(io.StringIO):
    """Typed answers on a terminal."""

    def isatty(self) -> bool:
        return True


def run(*argv: str, stdin: io.StringIO | None = None) -> tuple[int, str]:
    out = io.StringIO()
    code = rulescli.main(list(argv), stdout=out, stdin=stdin or io.StringIO())
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
    assert len(errors) == 1 and errors[0].startswith("error: line 22: ") and "@nope" in errors[0], out
    gone = next(line for line in out.splitlines() if "/gone" in line)
    assert gone.startswith("warning: line 18: ") and "does not exist" in gone
    assert "OK" not in out


def test_check_passes_with_a_missing_folder_as_a_warning(cfg, tmp_path):
    write(cfg, PINEAPPLE + '[sessions."{tmp}/gone"]\nrepo = "me/gone"\n')
    code, out = run("check")
    assert code == 0, out
    assert any(
        line.startswith("warning: ") and "does not exist" in line and f"remove-session {tmp_path}/gone" in line
        for line in out.splitlines()
    )
    assert "OK: 4 sessions" in out


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
    assert code == 0 and "check" in out and "show" in out and "grant" in out and "migrate" in out


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


def test_remove_session_drops_a_session_whose_folder_is_gone(cfg, tmp_path):
    write(cfg, PINEAPPLE + '[sessions."{tmp}/gone"]\nrepo = "me/gone"\n')
    assert len(load_rules(cfg).sessions) == 4
    assert run("remove-session", f"{tmp_path}/gone")[0] == 0
    assert len(load_rules(cfg).sessions) == 3


def test_add_session_for_a_missing_folder_writes_it_with_a_warning(cfg, tmp_path):
    code, out = run("add-session", f"{tmp_path}/later", "--repo", "me/x")
    assert code == 0 and "warning: " in out and "does not exist" in out


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


# --- migrate -----------------------------------------------------------------------------------


@pytest.fixture
def v1(cfg, tmp_path):
    """A v1 file with one workspace of each kind."""

    def checkout(name: str, url: str | None) -> Path:
        path = tmp_path / "ws" / name
        path.mkdir(parents=True)
        git("init", "-q", cwd=path)
        if url:
            git("remote", "add", "origin", url, cwd=path)
        return path

    ws = {
        "skills": checkout("skills", "git@github.com:Me/Skills.git"),
        "core": checkout("core", "https://github.com/me/core"),
        "unlisted": checkout("unlisted", "https://x-access-token:SECRET@github.com/me/unlisted.git"),
        "gitlab": checkout("gitlab", "https://gitlab.com/me/skills.git"),
        "bare": checkout("bare", None),
        "plain": tmp_path / "ws/skills/plain",  # inside a checkout, but not one itself
    }
    ws["plain"].mkdir()
    folders = [*map(str, ws.values()), str(tmp_path / "ws/missing"), str(ws["skills"]) + "/."]
    write(
        cfg,
        f'protected_branches = ["main", "release/*"]\nworkspaces = {json.dumps(folders)}\n'
        "[repos]\n"
        '"me/skills" = { access = "write", handover_paths = ["skills/**"], push_deploys = true }\n'
        '"me/flow" = "write"\n'
        '"me/core" = "read"\n'
        '"up/x" = { access = "read" }\n',
    )
    return ws


def test_migrate_shows_the_result_and_every_judgement_without_writing(cfg, v1, tmp_path):
    before = (cfg / "rules.toml").read_text()
    code, out = run("migrate")
    assert code == 0, out
    assert (cfg / "rules.toml").read_text() == before and backups(cfg) == []
    assert "gitgaterules migrate --write" in out
    assert 'reads = ["me/core", "up/x"]' in out
    lines = out.splitlines()

    def said(folder: str, *words: str) -> None:
        found = [line for line in lines if line.startswith(f"- {folder}") and all(w in line for w in words)]
        assert found, (folder, words, out)

    said(str(v1["skills"]), "Me/Skills", "proposed as the session's own repo")
    assert "asked one by one" in out
    said(str(v1["core"]), "me/core", "read-only", f"gitgaterules grant {v1['core']} write me/core")
    said(str(v1["unlisted"]), "me/unlisted", "not in [repos]", "left out")
    said(str(v1["gitlab"]), "not a github.com")
    said(str(v1["bare"]), "no origin")
    said(str(v1["plain"]), "not a git checkout")
    said(str(tmp_path / "ws/missing"), "does not exist")
    said(str(v1["skills"]) + "/.", "same folder")
    said("me/flow", "no workspace", "left out")
    assert "SECRET" not in out


def test_migrate_write_makes_a_v2_file_that_keeps_todays_access(cfg, v1):
    code, out = run("migrate", "--write", "--yes-pairings", f"{v1['skills']}=Me/Skills")
    assert code == 0, out
    assert len(backups(cfg)) == 1
    rules = load_rules(cfg)
    assert rules.version == 2 and rules.protected_branches == ("main", "release/*")
    skills = rules.scoped(str(v1["skills"]))
    assert {n: r.access for n, r in skills.repos.items()} == {"me/skills": "write", "me/core": "read", "up/x": "read"}
    assert "skills" in skills.repo("me/skills").handover_paths and skills.repo("me/skills").push_deploys
    core = rules.scoped(str(v1["core"]))
    assert {n: r.access for n, r in core.repos.items()} == {"me/core": "read", "up/x": "read"}
    assert set(rules.scoped(str(v1["plain"])).repos) == {"me/core", "up/x"}  # its own session, inside skills
    assert len(rules.sessions) == 6
    assert run("check")[0] == 0


def test_migrate_reads_only_the_workspaces_own_git_config(cfg, v1, tmp_path, monkeypatch):
    """The user's global config is not read: an insteadOf there would otherwise name another repo."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text('[remote "origin"]\n\turl = https://github.com/evil/repo\n')
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))
    code, out = run("migrate")
    assert "evil" not in out
    assert any(line.startswith(f"- {v1['bare']}") and "no origin" in line for line in out.splitlines())


def test_migrate_leaves_a_v2_file_alone(cfg):
    write(cfg, PINEAPPLE)
    code, out = run("migrate")
    assert code == 0 and "already" in out and backups(cfg) == []


def test_migrate_refuses_a_broken_v1_file(cfg):
    write(cfg, 'workspaces = ["rel"]\n')
    code, out = run("migrate")
    assert code == 1 and "absolute" in out


def test_a_rewrite_drops_only_entries_the_gate_treats_as_the_same(cfg, tmp_path):
    """Each list is deduplicated with the gate's own comparison for that key, never a looser one."""
    write(
        cfg,
        'protected_branches = ["release/*", "Release/*", "main", "main"]\n'
        "[groups]\n"
        'g = ["Me/X", "me/x", "@h"]\n'
        'h = ["up/y"]\n'
        '[sessions."{tmp}/core"]\n'
        'read = ["@g", "UP/Z", "up/z"]\n'
        '[repos."me/x"]\n'
        'handover_paths = ["Skills/**", "skills", "Docs/*.MD", "docs/*.md", "Other"]\n',
    )
    before = load_rules(cfg)
    assert run("grant", f"{tmp_path}/core", "write", "me/w")[0] == 0
    text = (cfg / "rules.toml").read_text()
    assert 'protected_branches = ["main", "Release/*", "release/*"]' in text
    assert 'g = ["@h", "Me/X"]' in text
    assert 'read = ["@g", "UP/Z"]' in text
    after = load_rules(cfg)
    assert after.protected_branches == ("main", "Release/*", "release/*")
    old, new = before.scoped(f"{tmp_path}/core"), after.scoped(f"{tmp_path}/core")
    assert set(new.repo("me/x").handover_paths) == set(old.repo("me/x").handover_paths)
    assert {n: r.access for n, r in new.repos.items()} == {
        **{n: r.access for n, r in old.repos.items()}, "me/w": "write",
    }  # fmt: skip


# --- migrate asks before it pairs a folder with a repo ------------------------------------------
# A workspace's git config is the agent's to change, so its origin is only ever a suggestion.


def owned(cfg) -> dict[str, str | None]:
    sessions = tomllib.loads((cfg / "rules.toml").read_text())["sessions"]
    return {Path(k).name: v.get("repo") for k, v in sessions.items()}


def test_migrate_write_without_a_terminal_needs_the_pairings_named(cfg, v1):
    before = (cfg / "rules.toml").read_text()
    code, out = run("migrate", "--write")
    assert code == 1 and "--yes-pairings" in out and f"{v1['skills']}=Me/Skills" in out
    assert (cfg / "rules.toml").read_text() == before and backups(cfg) == []


def test_named_pairings_must_be_proposed_ones(cfg, v1):
    before = (cfg / "rules.toml").read_text()
    code, out = run("migrate", "--write", "--yes-pairings", f"{v1['core']}=me/flow")
    assert code == 1 and "not a proposed pairing" in out
    assert (cfg / "rules.toml").read_text() == before
    assert run("migrate", "--yes-pairings", f"{v1['skills']}=Me/Skills")[0] == 2  # only with --write


def test_an_unconfirmed_pairing_leaves_the_session_without_a_repo(cfg, v1):
    code, out = run("migrate", "--write", "--yes-pairings")
    assert code == 0, out
    assert owned(cfg)["skills"] is None
    assert "Me/Skills" in out and "not paired" in out


def test_each_pairing_is_asked_with_no_as_the_default(cfg, v1):
    code, out = run("migrate", "--write", stdin=Terminal("\n"))
    assert code == 0, out
    assert f"Make Me/Skills the own repo (write) of {v1['skills']}? [y/N]" in out
    assert owned(cfg)["skills"] is None
    (cfg / "rules.toml").write_text(sorted(cfg.glob("rules.toml.bak-*"))[0].read_text())
    assert run("migrate", "--write", stdin=Terminal("y\n"))[0] == 0
    assert owned(cfg)["skills"] == "Me/Skills"


def two_claims(cfg, tmp_path, *, setup) -> tuple[Path, Path]:
    a, b = tmp_path / "a/skills", tmp_path / "b/skills"
    setup(a, b)
    write(cfg, f'workspaces = ["{a}", "{b}"]\n[repos]\n"me/skills" = "write"\n')
    return a, b


def checkout_of(path: Path, url: str) -> None:
    path.mkdir(parents=True)
    git("init", "-q", cwd=path)
    git("remote", "add", "origin", url, cwd=path)


def test_a_repo_claimed_by_two_folders_needs_its_name_typed(cfg, tmp_path):
    a, b = two_claims(
        cfg, tmp_path, setup=lambda a, b: [checkout_of(p, "https://github.com/me/skills") for p in (a, b)]
    )
    code, out = run("migrate")
    assert out.count("claimed by more than one folder") >= 2
    code, out = run("migrate", "--write", stdin=Terminal("y\nme/skills\n"))
    assert code == 0, out
    assert "Type me/skills to pair them" in out
    sessions = tomllib.loads((cfg / "rules.toml").read_text())["sessions"]
    assert sessions[str(a)].get("repo") is None and sessions[str(b)]["repo"] == "me/skills"


def test_a_git_dir_outside_the_folder_is_flagged(cfg, tmp_path):
    def setup(a, b):
        main = tmp_path / "main"
        checkout_of(main, "https://github.com/me/skills")
        git("commit", "-q", "--allow-empty", "-m", "x", cwd=main)
        a.parent.mkdir(parents=True)
        git("worktree", "add", "-q", str(a), cwd=main)  # .git is a gitfile into main/.git
        b.mkdir(parents=True)
        (b / ".git").symlink_to(main / ".git")

    a, b = two_claims(cfg, tmp_path, setup=setup)
    write(cfg, f'workspaces = ["{a}"]\n[repos]\n"me/skills" = "write"\n')
    code, out = run("migrate")
    assert "outside the folder" in out
    code, out = run("migrate", "--write", stdin=Terminal("y\n"))
    assert code == 0 and owned(cfg)["skills"] is None
    write(cfg, f'workspaces = ["{b}"]\n[repos]\n"me/skills" = "write"\n')
    code, out = run("migrate")
    assert "symlink" in out and "outside the folder" in out


def test_a_folder_named_unlike_its_repo_is_warned_about(cfg, tmp_path):
    folder = tmp_path / "ws/elsewhere"
    checkout_of(folder, "https://github.com/me/skills")
    write(cfg, f'workspaces = ["{folder}"]\n[repos]\n"me/skills" = "write"\n')
    code, out = run("migrate")
    assert "is named elsewhere" in out
    code, out = run("migrate", "--write", stdin=Terminal("y\n"))
    assert code == 0 and owned(cfg)["elsewhere"] == "me/skills"  # a warning, not a flag


# --- what gitgaterules prints is escaped ---------------------------------------------------------
# Remote URLs, git's stderr, folder names and the file itself can hold text an agent wrote.


def unprintable(text: str) -> list[str]:
    return [c for c in text if not c.isprintable() and c != "\n"]


def one_workspace(cfg, tmp_path, url: str, name: str = "w") -> Path:
    folder = tmp_path / "ws" / name
    checkout_of(folder, url)
    write(cfg, f'workspaces = [{json.dumps(str(folder))}]\n[repos]\n"me/x" = "write"\n')
    return folder


def test_an_escape_sequence_in_a_remote_url_is_escaped(cfg, tmp_path):
    one_workspace(cfg, tmp_path, "https://gitlab.com/me/\x1b]0;owned\x07x‮.git")
    code, out = run("migrate")
    assert unprintable(out) == []
    assert "\\x1b]0;owned\\x07x\\u202e.git" in out


def test_a_shown_url_keeps_no_credentials_query_or_fragment(cfg, tmp_path):
    one_workspace(cfg, tmp_path, "https://user:tok%40en@gitlab.com/me/x.git?private_token=abc#frag")
    code, out = run("migrate")
    assert "https://gitlab.com/me/x.git" in out
    for secret in ("user", "tok", "private_token", "abc", "frag"):
        assert secret not in out.replace("tmp/", "")
    assert rulescli.shown_url("git@evil@github.com:me/x?q#f") == "github.com:me/x"
    assert rulescli.shown_url("ssh://a:b@c@host:22/p/q.git?x") == "ssh://host:22/p/q.git"


def test_git_stderr_is_escaped_and_cut(cfg, tmp_path, monkeypatch):
    fake = tmp_path / "git"
    fake.write_text(
        "#!/bin/sh\nprintf 'fatal: \\033[2Jcleared\\r%s' \"$(printf 'x%.0s' $(seq 1 2000))\" >&2\nexit 128\n"
    )
    fake.chmod(0o755)
    monkeypatch.setattr(rulescli, "GIT", str(fake))
    one_workspace(cfg, tmp_path, "https://github.com/me/x")
    code, out = run("migrate")
    assert unprintable(out) == []
    line = next(line for line in out.splitlines() if "could not be read by git" in line)
    assert len(line) < 600 and "\\x1b[2Jcleared\\x0d" in line


def test_a_folder_name_with_control_characters_is_escaped(cfg, tmp_path):
    folder = tmp_path / "ws" / "a\x1b[31mb"
    folder.mkdir(parents=True)
    write(cfg, f"workspaces = [{json.dumps(str(folder))}]\n")
    code, out = run("migrate")
    assert unprintable(out) == [] and "a\\x1b[31mb" in out


def test_the_diff_and_the_file_hold_no_raw_invisible_characters(cfg, tmp_path):
    write(cfg, 'protected_branches = ["ma‮in"]\n' + PINEAPPLE)
    code, out = run("grant", f"{tmp_path}/core", "read", "up/x")
    assert code == 0 and unprintable(out) == []
    text = (cfg / "rules.toml").read_text()
    assert unprintable(text) == [] and "\\u202e" in text
    assert load_rules(cfg).protected_branches == ("ma‮in",)


def test_a_newline_in_a_value_cannot_start_a_line_of_its_own(cfg, tmp_path):
    """A folder named to print a fake "OK" line, or a fake pairing, stays on its own line, escaped."""
    folder = tmp_path / "ws" / "a\nOK: 1 sessions, 1 repos\n- b: paired."
    checkout_of(folder, "https://github.com/me/x")
    write(cfg, f'workspaces = [{json.dumps(str(folder))}]\n[repos]\n"me/x" = "write"\n')
    for argv in (("migrate",), ("show",), ("migrate", "--write", "--yes-pairings")):
        code, out = run(*argv)
        lines = out.splitlines()
        assert not any(line.startswith(("OK:", "- b:")) for line in lines), (argv, out)
        assert any("a\\x0aOK: 1 sessions, 1 repos\\x0a- b: paired." in line for line in lines), (argv, out)


def test_a_carriage_return_in_a_url_is_escaped_on_its_line(cfg, tmp_path):
    one_workspace(cfg, tmp_path, "https://gitlab.com/me/x\rOK: 1 sessions\n")
    code, out = run("migrate")
    line = next(line for line in out.splitlines() if "gitlab.com" in line)
    assert "me/x\\x0dOK: 1 sessions" in line
    assert not any(line.startswith("OK") for line in out.splitlines())


def test_a_problem_naming_a_folder_stays_on_one_line(cfg, tmp_path, monkeypatch):
    state = tmp_path / "x\nOK: v1 file, 0 workspaces, 0 repos"
    state.mkdir()
    monkeypatch.setenv("GIT_GATE_STATE_DIR", str(state))
    write(cfg, f"workspaces = [{json.dumps(str(state))}]\n")
    code, out = run("check")
    assert code == 1 and not any(line.startswith("OK") for line in out.splitlines())
    assert "x\\x0aOK: v1 file" in out
