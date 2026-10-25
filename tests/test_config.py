import pytest

from git_gate import Refused
from git_gate.config import DEFAULT_HANDOVER_PATHS, RepoRules, check_enabled, load_rules


def write_rules(tmp_path, repos_toml):
    (tmp_path / "rules.toml").write_text(f"workspaces = []\n[repos]\n{repos_toml}\n")
    return tmp_path


def test_default_handover_paths():
    assert DEFAULT_HANDOVER_PATHS == (".github/workflows/**", ".githooks/**", ".pre-commit-config.yaml")


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
    assert repo.handover_paths == DEFAULT_HANDOVER_PATHS + ("skills/**",)
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
