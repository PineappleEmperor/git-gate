"""The v2 rules file: sessions, groups, and each session's effective access."""

import pytest

from git_gate import Refused
from git_gate.config import DEFAULT_HANDOVER_PATHS, RepoRules, check_rules, load_rules


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_GATE_STATE_DIR", str(tmp_path / "state"))
    d = tmp_path / "cfg"
    d.mkdir()
    for name in ("a", "b", "a/inner"):
        (tmp_path / name).mkdir()
    return d


def write(cfg, text: str):
    (cfg / "rules.toml").write_text(text.replace("{tmp}", str(cfg.parent)))
    return cfg


def problems(cfg) -> list[str]:
    return [p.text for p in check_rules((cfg / "rules.toml").read_text(), cfg).problems]


SAMPLE = """
protected_branches = ["main"]
[groups]
reads = ["up/core", "@more"]
more = ["up/docs"]
[sessions."{tmp}/a"]
repo = "Me/App"
read = ["@reads"]
write = ["me/lib"]
[sessions."{tmp}/b"]
read = ["me/app"]
[repos."me/app"]
handover_paths = ["skills/**"]
push_deploys = true
"""


def test_effective_access_per_session(cfg):
    rules = load_rules(write(cfg, SAMPLE))
    assert rules.version == 2
    a = rules.scoped(str(cfg.parent / "a"))
    assert a.workspaces == ((cfg.parent / "a").resolve(),)
    assert {n: r.access for n, r in a.repos.items()} == {
        "me/app": "write", "me/lib": "write", "up/core": "read", "up/docs": "read",
    }  # fmt: skip
    assert a.repo("ME/app") == RepoRules("write", DEFAULT_HANDOVER_PATHS + ("skills",), True)
    assert a.repo("me/lib") == RepoRules("write", DEFAULT_HANDOVER_PATHS, False)
    b = rules.scoped(str(cfg.parent / "b"))
    assert {n: r.access for n, r in b.repos.items()} == {"me/app": "read"}
    assert b.protected_branches == ("main",)


def test_a_shared_group_may_hold_the_sessions_own_repo(cfg):
    """Every pineapple site reads every other: one group, granted to each, includes each one's own repo."""
    write(
        cfg,
        """
[groups]
pineapple = ["me/core", "me/crown", "me/frond"]
[sessions."{tmp}/a"]
repo = "me/core"
read = ["@pineapple"]
[sessions."{tmp}/b"]
repo = "me/crown"
read = ["@pineapple", "me/extra"]
write = ["me/frond", "me/extra2"]
""",
    )
    assert problems(cfg) == []
    rules = load_rules(cfg)
    a = rules.scoped(str(cfg.parent / "a"))
    assert {n: r.access for n, r in a.repos.items()} == {"me/core": "write", "me/crown": "read", "me/frond": "read"}
    b = rules.scoped(str(cfg.parent / "b"))
    assert {n: r.access for n, r in b.repos.items()} == {
        "me/core": "read", "me/crown": "write", "me/frond": "write", "me/extra": "read", "me/extra2": "write",
    }  # fmt: skip


def test_a_group_reached_by_both_read_and_write_resolves_to_write(cfg):
    write(
        cfg,
        """
[groups]
g = ["me/x", "me/y"]
[sessions."{tmp}/a"]
read = ["@g"]
write = ["me/x"]
""",
    )
    assert problems(cfg) == []
    a = load_rules(cfg).scoped(str(cfg.parent / "a"))
    assert {n: r.access for n, r in a.repos.items()} == {"me/x": "write", "me/y": "read"}


def test_a_session_without_repo_has_only_its_grants(cfg):
    write(cfg, '[sessions."{tmp}/a"]\nread = ["up/core"]\n')
    a = load_rules(cfg).scoped(str(cfg.parent / "a"))
    assert {n: r.access for n, r in a.repos.items()} == {"up/core": "read"}
    with pytest.raises(Refused, match="can only read up/core"):
        a.require_write("up/core")


def test_the_longest_folder_wins_and_a_worktree_belongs_to_its_folder(cfg):
    write(cfg, '[sessions."{tmp}/a"]\nrepo = "me/outer"\n[sessions."{tmp}/a/inner"]\nrepo = "me/inner"\n')
    rules = load_rules(cfg)
    wt = cfg.parent / "a/.tmp/wt-x/sub"
    wt.mkdir(parents=True)
    assert set(rules.scoped(str(wt)).repos) == {"me/outer"}
    assert set(rules.scoped(str(cfg.parent / "a/inner")).repos) == {"me/inner"}
    assert set(rules.scoped(str(cfg.parent / "a/inner/deeper")).repos) == {"me/inner"}
    # "a" is a folder prefix of "ab" as text only: it is not the same folder.
    with pytest.raises(Refused, match="not a git-gate session"):
        rules.scoped(str(cfg.parent / "ab"))


def test_no_known_folder_or_no_session_gets_nothing(cfg):
    rules = load_rules(write(cfg, '[sessions."{tmp}/a"]\nrepo = "me/app"\n'))
    with pytest.raises(Refused) as e:
        rules.scoped(None)
    assert "gitgaterules" in str(e.value)
    other = str(cfg.parent / "b")
    with pytest.raises(Refused) as e:
        rules.scoped(other)
    assert other in str(e.value) and "gitgaterules add-session" in str(e.value)
    # Unscoped v2 rules hold no repos and no workspaces: fail closed by construction.
    assert rules.repos == {} and rules.workspaces == ()
    with pytest.raises(Refused):
        rules.repo("me/app")


def test_refusals_name_the_session_and_the_command(cfg):
    a = load_rules(write(cfg, '[sessions."{tmp}/a"]\nread = ["up/core"]\n')).scoped(str(cfg.parent / "a"))
    folder = (cfg.parent / "a").resolve()
    with pytest.raises(Refused) as e:
        a.repo("me/other")
    assert f"This session ({folder}) has no access to me/other" in str(e.value)
    assert f"gitgaterules grant {folder} read me/other" in str(e.value)
    with pytest.raises(Refused) as e:
        a.require_write("up/core")
    assert f"gitgaterules grant {folder} write up/core" in str(e.value)


def test_access_level_for_the_audit(cfg):
    rules = load_rules(write(cfg, '[sessions."{tmp}/a"]\nrepo = "me/app"\nread = ["up/core"]\n'))
    a = rules.scoped(str(cfg.parent / "a"))
    assert [a.level(r) for r in ("me/app", "UP/core", "me/x", "not a repo", None)] == [
        "write", "read", "none", "none", None,
    ]  # fmt: skip


def test_v1_is_unchanged_and_unscoped(cfg):
    v1 = write(cfg, f'workspaces = ["{cfg.parent / "a"}"]\n[repos]\n"me/app" = "write"\n')
    rules = load_rules(v1)
    assert rules.version == 1
    assert rules.scoped(None) is rules and rules.scoped("/anywhere") is rules
    assert rules.level("me/app") is None
    checked = check_rules((cfg / "rules.toml").read_text(), cfg)
    assert checked.problems == [] and any("v1" in w.text for w in checked.warnings)


@pytest.mark.parametrize(
    ("toml", "words"),
    [
        ('[sessions."{tmp}/a"]\nrepo = "me/app"\nreed = []\n', ['[sessions."{tmp}/a"]', "reed"]),
        ('workspaces = []\n[sessions."{tmp}/a"]\n', ["workspaces"]),
        ('[groups]\ng = ["x"]\n[sessions."{tmp}/a"]\n', ["[groups] g", "'x'", "owner/name"]),
        ('[sessions."{tmp}/a"]\nrepo = "me/app.git"\n', ["repo", "'me/app.git'"]),
        ('[sessions."{tmp}/a"]\nrepo = ["me/app"]\n', ["repo"]),
        ('[sessions."{tmp}/a"]\nread = "me/app"\n', ["read", "list"]),
        ('[sessions."{tmp}/a"]\nwrite = ["me/app\\n"]\n', ["write", "owner/name"]),
        ('[sessions."{tmp}/a"]\nread = ["@nope"]\n', ["read", "@nope", "[groups]"]),
        ('[groups]\na = ["@b"]\nb = ["@a"]\n[sessions."{tmp}/a"]\n', ["[groups]", "includes itself"]),
        ('[groups]\na = ["@a"]\n[sessions."{tmp}/a"]\n', ["[groups] a", "includes itself"]),
        ('[groups]\n"bad name" = []\n[sessions."{tmp}/a"]\n', ["'bad name'"]),
        ('[groups]\ng = 5\n[sessions."{tmp}/a"]\n', ["[groups] g", "list"]),
        ('[sessions."rel/a"]\n', ['[sessions."rel/a"]', "absolute"]),
        ('[sessions."{tmp}/cfg/x"]\n', ["gate-owned"]),
        ('[sessions."{tmp}"]\n', ["gate-owned"]),
        ('[sessions."{tmp}/state"]\n', ["gate-owned"]),
        ('[sessions."{tmp}/a"]\n[sessions."{tmp}/./a"]\n', ["same folder"]),
        (
            '[sessions."{tmp}/a"]\nread = ["me/app"]\nwrite = ["Me/App"]\n',
            ['[sessions."{tmp}/a"]', "me/app", "both read and write"],
        ),
        ('[sessions."{tmp}/a"]\n[repos."me/app"]\naccess = "write"\n', ['[repos."me/app"]', "access"]),
        ('[sessions."{tmp}/a"]\n[repos]\n"me/app" = "write"\n', ['[repos."me/app"]']),
        ('[sessions."{tmp}/a"]\n[repos."me/app"]\nsurprise = 1\n', ['[repos."me/app"]', "surprise"]),
        ('[sessions."{tmp}/a"]\n[repos."me/app"]\nhandover_paths = ["a/../b"]\n', ['[repos."me/app"]', "'..'"]),
        ("sessions = 5\n", ["sessions", "table"]),
        ('[sessions."{tmp}/a"]\nrepo = "me/app"\ngroups = 1\n', ["groups"]),
    ],
)
def test_v2_problems_name_the_key(cfg, toml, words):
    (cfg.parent / "state").mkdir(exist_ok=True)
    write(cfg, toml)
    with pytest.raises(Refused) as e:
        load_rules(cfg)
    for word in words:
        assert word.replace("{tmp}", str(cfg.parent)) in str(e.value)


def test_every_problem_is_reported(cfg):
    write(cfg, '[sessions."{tmp}/a"]\nrepo = "bad"\nread = ["@nope"]\n[sessions."rel"]\n')
    found = problems(cfg)
    assert len(found) == 3
    with pytest.raises(Refused) as e:
        load_rules(cfg)
    assert str(e.value).count(str(cfg / "rules.toml")) == 3


def test_an_empty_sessions_table_is_v2_with_no_access(cfg):
    rules = load_rules(write(cfg, "[sessions]\n"))
    assert rules.version == 2
    with pytest.raises(Refused, match="not a git-gate session"):
        rules.scoped(str(cfg.parent / "a"))


def test_warnings(cfg):
    (cfg.parent / "b/.git").mkdir()
    write(cfg, '[sessions."{tmp}/a"]\n[sessions."{tmp}/b"]\n[repos."me/unused"]\npush_deploys = true\n')
    checked = check_rules((cfg / "rules.toml").read_text(), cfg)
    assert checked.problems == []
    texts = [w.text for w in checked.warnings]
    assert len(texts) == 2
    assert any(str(cfg.parent / "a") in t and "not a git checkout" in t for t in texts)
    assert any("me/unused" in t and "no session" in t for t in texts)


def test_a_missing_folder_is_a_warning_and_its_session_matches_nothing(cfg):
    """Deleting one project folder must not refuse every other session's calls."""
    (cfg.parent / "file").write_text("")
    write(
        cfg,
        """
[sessions."{tmp}/a"]
repo = "me/app"
[sessions."{tmp}/gone"]
repo = "me/gone"
[sessions."{tmp}/file"]
repo = "me/file"
""",
    )
    checked = check_rules((cfg / "rules.toml").read_text(), cfg)
    assert checked.problems == []
    for name in ("gone", "file"):
        assert any(
            str(cfg.parent / name) in w.text and "does not exist" in w.text and "remove-session" in w.text
            for w in checked.warnings
        ), checked.warnings
    rules = load_rules(cfg)
    assert set(rules.scoped(str(cfg.parent / "a")).repos) == {"me/app"}
    with pytest.raises(Refused, match="not a git-gate session"):
        rules.scoped(str(cfg.parent / "gone"))
    with pytest.raises(Refused, match="not a git-gate session"):
        rules.scoped(str(cfg.parent / "gone/sub"))


def test_a_missing_folders_session_is_still_checked(cfg):
    write(cfg, '[sessions."{tmp}/gone"]\nrepo = "bad"\nread = ["@nope"]\n')
    assert len(problems(cfg)) == 2
    write(cfg, '[sessions."{tmp}/cfg/gone"]\n')
    assert any("gate-owned" in p for p in problems(cfg))
