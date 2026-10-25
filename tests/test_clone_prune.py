import pytest

from conftest import call, commit, git, push
from git_gate import Refused

# --- clone ---------------------------------------------------------------------------------


def test_clone_leaves_head_on_default_and_checks_out_in_sandbox(env):
    git("branch", "feature", cwd=env.bare)
    out = call(env, "clone", repo="me/app", path=str(env.ws / "new"))
    assert "reset --hard" in out
    path = env.ws / "new"
    assert git("symbolic-ref", "HEAD", cwd=path) == "refs/heads/main"
    assert git("config", "remote.origin.url", cwd=path) == env.url
    assert not (path / "README.md").exists()
    assert not list((path / ".git").glob("hooks/*"))
    git("reset", "-q", "--hard", cwd=path)
    assert (path / "README.md").read_text() == "hello"
    assert "origin/feature" in git("branch", "-r", cwd=path)


def test_clone_empty_remote(env):
    git("init", "-q", "--bare", str(env.remote / "me/empty.git"), cwd=env.tmp)
    (env.cfg / "rules.toml").write_text(f'workspaces = ["{env.ws}"]\n[repos]\n"me/empty" = "write"\n')
    assert "empty repository" in call(env, "clone", repo="me/empty", path=str(env.ws / "e"))
    assert (env.ws / "e/.git").is_dir()


def test_clone_refuses_existing_nonempty_dir(env):
    (env.ws / "busy").mkdir()
    (env.ws / "busy/f").write_text("x")
    with pytest.raises(Refused, match="already exists"):
        call(env, "clone", repo="me/app", path=str(env.ws / "busy"))


def test_clone_refuses_outside_workspace(env):
    with pytest.raises(Refused, match="outside the workspaces"):
        call(env, "clone", repo="me/app", path=str(env.tmp / "elsewhere"))


def test_clone_refuses_symlinked_parent(env):
    (env.ws / "link").symlink_to(env.tmp)
    (env.tmp / "sub").mkdir()
    with pytest.raises(Refused, match="Not a directory"):
        call(env, "clone", repo="me/app", path=str(env.ws / "link" / "x"))
    with pytest.raises(Refused, match="outside the workspaces"):
        call(env, "clone", repo="me/app", path=str(env.ws / "link" / "sub" / "x"))


def test_unlisted_repo_refused(env):
    with pytest.raises(Refused, match="allowlist"):
        call(env, "clone", repo="me/secret", path=str(env.ws / "s"))


# --- prune ---------------------------------------------------------------------------------


def _pushed_branch(env, checkout, name="done"):
    git("switch", "-q", "-c", name, cwd=checkout)
    sha = commit(checkout, f"{name}.txt")
    push(env, checkout, name)
    return sha


def test_prune_merged_branch(env, checkout):
    sha = _pushed_branch(env, checkout)
    env.api.pulls["done"] = [{"number": 4, "state": "closed", "closed_at": "2026-10-01", "head_sha": sha}]
    assert "Deleted done" in call(env, "prune", repo="me/app", branch="done")
    assert env.api.deleted == [("me/app", "done")]


def test_prune_refuses_open_missing_or_moved(env, checkout):
    sha = _pushed_branch(env, checkout)
    with pytest.raises(Refused, match="No pull request"):
        call(env, "prune", repo="me/app", branch="done")
    env.api.pulls["done"] = [
        {"number": 4, "state": "closed", "closed_at": "2026-10-01", "head_sha": sha},
        {"number": 5, "state": "open", "closed_at": "", "head_sha": sha},
    ]
    with pytest.raises(Refused, match="open pull request"):
        call(env, "prune", repo="me/app", branch="done")
    env.api.pulls["done"] = [{"number": 4, "state": "closed", "closed_at": "2026-10-01", "head_sha": "0" * 40}]
    with pytest.raises(Refused, match="has moved"):
        call(env, "prune", repo="me/app", branch="done")
    assert env.api.deleted == []


def test_prune_refuses_protected(env):
    with pytest.raises(Refused, match="default branch"):
        call(env, "prune", repo="me/app", branch="main")
    with pytest.raises(Refused, match="protected pattern"):
        call(env, "prune", repo="me/app", branch="release/2")
