import json
import re

import pytest

from git_gate import Refused, mirror
from git_gate.config import DEFAULT_HANDOVER_PATHS, RepoRules, canonical_pattern, check_enabled, load_rules


def write_rules(tmp_path, repos_toml):
    (tmp_path / "rules.toml").write_text(f"workspaces = []\n[repos]\n{repos_toml}\n")
    return tmp_path


def test_default_handover_paths():
    assert DEFAULT_HANDOVER_PATHS == (
        ".github/workflows", ".githooks", ".pre-commit-config.yaml", ".claude", ".mcp.json", ".vscode", ".idea",
        ".envrc", ".devcontainer",
    )  # fmt: skip


def test_string_form_still_works(tmp_path):
    rules = load_rules(write_rules(tmp_path, '"me/app" = "write"'))
    assert rules.repo("me/app") == RepoRules("write", DEFAULT_HANDOVER_PATHS, False)
    assert rules.access("ME/App") == "write"
    rules.require_write("me/app")


def test_table_form(tmp_path):
    cfg = write_rules(
        tmp_path,
        '"me/app" = { access = "write", handover_paths = ["skills/**"], push_deploys = true }',
    )
    repo = load_rules(cfg).repo("me/app")
    assert repo.access == "write"
    assert repo.handover_paths == DEFAULT_HANDOVER_PATHS + ("skills",)
    assert repo.push_deploys is True


def test_table_form_defaults(tmp_path):
    repo = load_rules(write_rules(tmp_path, '"me/app" = { access = "read" }')).repo("me/app")
    assert repo == RepoRules("read", DEFAULT_HANDOVER_PATHS, False)


@pytest.mark.parametrize(
    "entry",
    [
        '{ access = "write", surprise = 1 }',
        '{ access = "admin" }',
        '{ handover_paths = ["x"] }',
        '{ access = "write", handover_paths = "skills/**" }',
        '{ access = "write", handover_paths = [1] }',
        '{ access = "write", push_deploys = "yes" }',
    ],
)
def test_bad_table_refused(tmp_path, entry):
    with pytest.raises(Refused, match="me/app"):
        load_rules(write_rules(tmp_path, f'"me/app" = {entry}'))


def test_bad_string_refused(tmp_path):
    with pytest.raises(Refused, match="me/app"):
        load_rules(write_rules(tmp_path, '"me/app" = "admin"'))


def test_unlisted_repo_refused(tmp_path):
    rules = load_rules(write_rules(tmp_path, '"me/app" = "read"'))
    with pytest.raises(Refused, match="allowlist"):
        rules.repo("me/other")
    with pytest.raises(Refused, match="read-only"):
        rules.require_write("me/app")


def test_off_switch(tmp_path):
    assert check_enabled(tmp_path) is None
    (tmp_path / "disabled").write_text("")
    with pytest.raises(Refused) as e:
        check_enabled(tmp_path)
    assert str(e.value) == "git-gate is switched off: remove ~/.config/git-gate/disabled to turn it back on."


# --- handover patterns are canonical --------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("skills", "skills"),
        ("skills/", "skills"),
        ("skills/**", "skills"),
        ("/Skills/**", "skills"),
        ("./skills//**", "skills"),
        (".github/workflows/**", ".github/workflows"),
        ("**", "**"),
        ("/", "**"),
        ("skills/*/SKILL.md", "skills/*/skill.md"),
    ],
)
def test_handover_patterns_are_canonical(tmp_path, raw, canonical):
    assert canonical_pattern(raw) == canonical
    cfg = write_rules(tmp_path, f'"me/app" = {{ access = "write", handover_paths = [{json.dumps(raw)}] }}')
    paths = load_rules(cfg).repo("me/app").handover_paths
    assert canonical in paths and len(paths) == len(set(paths))


@pytest.mark.parametrize("raw", ["a/../b", "a\\b", "a/**b", "", "a\x00b", "a\nb", "a\u202eb"])
def test_bad_handover_patterns_refused(tmp_path, raw):
    cfg = write_rules(tmp_path, f'"me/app" = {{ access = "write", handover_paths = [{json.dumps(raw)}] }}')
    with pytest.raises(Refused) as e:
        load_rules(cfg)
    assert "me/app" in str(e.value) and repr(raw) in str(e.value)
    with pytest.raises(Refused):
        canonical_pattern(raw)


# --- the rules schema is strict -------------------------------------------------------------


@pytest.mark.parametrize(
    ("toml", "key"),
    [
        ('workspaces = []\nprotected_branches = "main"\n[repos]\n', "protected_branches"),
        ("workspaces = []\nprotected_branch = []\n[repos]\n", "protected_branch"),
        ('workspaces = "/x"\n[repos]\n', "workspaces"),
        ("workspaces = []\ngit_timeout_seconds = true\n[repos]\n", "git_timeout_seconds"),
        ("workspaces = []\ngit_timeout_seconds = 0\n[repos]\n", "git_timeout_seconds"),
        ('workspaces = []\ngit_timeout_seconds = "5"\n[repos]\n', "git_timeout_seconds"),
        ("workspaces = []\ngh_timeout_seconds = 86401\n[repos]\n", "gh_timeout_seconds"),
        ("workspaces = [5]\n[repos]\n", "workspaces"),
        ('workspaces = []\nprotected_branches = [""]\n[repos]\n', "protected_branches"),
        ("workspaces = []\nrepos = 5\n", "repos"),
        ('workspaces = []\n[repos]\n"Me/App" = "write"\n"me/app" = "read"\n', "me/app"),
        ('workspaces = []\n[repos]\n"me/app\\n" = "write"\n', "me/app"),
    ],
)
def test_rules_schema_is_strict(tmp_path, toml, key):
    (tmp_path / "rules.toml").write_text(toml)
    with pytest.raises(Refused, match=re.compile(re.escape(key), re.IGNORECASE)):
        load_rules(tmp_path)


def test_timeouts_in_range_load(tmp_path):
    (tmp_path / "rules.toml").write_text("workspaces = []\ngit_timeout_seconds = 1\ngh_timeout_seconds = 86400\n")
    rules = load_rules(tmp_path)
    assert (rules.git_timeout, rules.gh_timeout, rules.repos) == (1, 86400, {})


def test_identifiers_fullmatch(tmp_path):
    rules = load_rules(write_rules(tmp_path, '"me/app" = "write"'))
    with pytest.raises(Refused, match="not an owner/name"):
        rules.repo("me/app\n")
    sha = "a" * 40
    assert mirror._oid(sha) == sha
    with pytest.raises(Refused):
        mirror._oid(sha + "\n")
    with pytest.raises(Refused):
        mirror._handover_ref("refs/gate/handover/7\n")


def test_off_switch_dangling_symlink(tmp_path):
    """An off switch must fail closed: a symlink to nothing still switches the gate off."""
    (tmp_path / "disabled").symlink_to(tmp_path / "missing")
    with pytest.raises(Refused) as e:
        check_enabled(tmp_path)
    assert str(e.value) == "git-gate is switched off: remove ~/.config/git-gate/disabled to turn it back on."


def test_check_enabled_defaults_to_config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_GATE_CONFIG_DIR", str(tmp_path))
    assert check_enabled() is None
    (tmp_path / "disabled").touch()
    with pytest.raises(Refused, match="switched off"):
        check_enabled()
