"""A call reaches only the repos and the folder of the session it comes from (v2 rules)."""

import pytest

from conftest import call, commit, git, push
from git_gate import Refused, gate


@pytest.fixture
def scoped(env, monkeypatch):
    """me/app's own session in ws, reading other/lib; a second session that may only read me/app."""
    env.other = env.tmp / "other"
    env.other.mkdir()
    (env.cfg / "rules.toml").write_text(
        'protected_branches = ["main", "release/*"]\n'
        f'[sessions."{env.ws}"]\nrepo = "me/app"\nread = ["other/lib"]\n'
        f'[sessions."{env.other}"]\nread = ["me/app"]\n'
    )
    monkeypatch.chdir(env.ws)
    return env


def test_a_session_reaches_its_own_repo(scoped, checkout):
    assert "Cloned me/app" in call(scoped, "clone", repo="me/app", path=str(scoped.ws / "c"))
    git("checkout", "-q", "-b", "feat", cwd=checkout)
    commit(checkout, "f", "y")
    assert "Pushed" in push(scoped, checkout, "feat")


def test_a_worktree_under_the_folder_is_the_same_session(scoped, monkeypatch):
    wt = scoped.ws / ".tmp/wt-x"
    wt.mkdir(parents=True)
    monkeypatch.chdir(wt)
    assert "Cloned me/app" in call(scoped, "clone", repo="me/app", path=str(scoped.ws / "c"))


def test_a_folder_that_is_no_session_gets_nothing(scoped, monkeypatch):
    monkeypatch.chdir(scoped.tmp)
    with pytest.raises(Refused) as e:
        call(scoped, "clone", repo="me/app", path=str(scoped.ws / "c"))
    assert f"{scoped.tmp}, is not a git-gate session" in str(e.value)
    assert "gitgaterules add-session" in str(e.value)
    assert not (scoped.ws / "c").exists()


def test_an_unknown_folder_gets_nothing(scoped, monkeypatch):
    def gone():
        raise FileNotFoundError

    monkeypatch.setattr(gate.os, "getcwd", gone)
    with pytest.raises(Refused, match="cannot tell which project folder"):
        call(scoped, "clone", repo="me/app", path=str(scoped.ws / "c"))


def test_an_ungranted_repo_is_refused_with_the_grant_command(scoped):
    with pytest.raises(Refused) as e:
        call(scoped, "clone", repo="nobody/x", path=str(scoped.ws / "c"))
    assert f"This session ({scoped.ws}) has no access to nobody/x" in str(e.value)
    assert f"gitgaterules grant {scoped.ws} read nobody/x" in str(e.value)


def test_a_read_grant_reads_but_does_not_write(scoped, monkeypatch):
    monkeypatch.chdir(scoped.other)
    (scoped.other / ".tmp").mkdir()
    bundle = scoped.other / ".tmp/f.bundle"
    assert "Wrote 1 branch(es)" in call(scoped, "fetch", repo="me/app", bundle=str(bundle))
    with pytest.raises(Refused, match=f"This session \\({scoped.other}\\) can only read me/app"):
        call(scoped, "prune", repo="me/app", branch="feat")


def test_files_are_exchanged_only_inside_the_sessions_folder(scoped):
    """The other session's folder is a workspace of its own, not of this one."""
    (scoped.other / ".tmp").mkdir()
    with pytest.raises(Refused, match=f"outside this session's folder, {scoped.ws}"):
        call(scoped, "fetch", repo="me/app", bundle=str(scoped.other / ".tmp/f.bundle"))
    with pytest.raises(Refused, match="outside this session's folder"):
        call(scoped, "clone", repo="me/app", path=str(scoped.other / "c"))


def test_the_agent_cannot_name_its_own_folder(scoped):
    with pytest.raises(Refused, match="unexpected keyword argument 'cwd'"):
        call(scoped, "clone", repo="other/lib", path=str(scoped.ws / "c"), cwd=str(scoped.other))


def test_a_missing_session_folder_does_not_stop_the_other_sessions(scoped):
    rules = scoped.cfg / "rules.toml"
    rules.write_text(rules.read_text() + f'[sessions."{scoped.tmp}/deleted"]\nrepo = "me/gone"\n')
    assert "Cloned me/app" in call(scoped, "clone", repo="me/app", path=str(scoped.ws / "c"))
