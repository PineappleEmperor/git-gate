import json
import shutil

import pytest

from conftest import TOKEN, call, commit, git
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


# --- repo validation -----------------------------------------------------------------------


def test_fetch_refuses_gitdir_file(env, checkout):
    (env.ws / "wt").mkdir()
    (env.ws / "wt/.git").write_text(f"gitdir: {checkout}/.git\n")
    with pytest.raises(Refused, match="must be a directory"):
        call(env, "fetch", repo="me/app", path=str(env.ws / "wt"))


@pytest.mark.parametrize(
    "key,value",
    [
        ("core.fsmonitor", "touch /tmp/pwned"),
        ("core.sshCommand", "evil"),
        ("url.file:///evil/.insteadOf", "file://"),
        ("include.path", "/tmp/x"),
        ("credential.helper", "store"),
        ("remote.origin.pushurl", "file:///evil"),
        ("filter.x.smudge", "evil"),
    ],
)
def test_config_key_allowlist(env, checkout, key, value):
    git("config", key, value, cwd=checkout)
    with pytest.raises(Refused, match="allowlist"):
        call(env, "fetch", repo="me/app", path=str(checkout))


def test_remote_url_must_match_repo(env, checkout):
    git("config", "remote.origin.url", "https://github.com/other/lib.git", cwd=checkout)
    with pytest.raises(Refused, match="remote.origin.url"):
        call(env, "fetch", repo="me/app", path=str(checkout))
    with pytest.raises(Refused, match="remote.origin.url"):
        call(env, "fetch", repo="other/lib", path=str(checkout))


def test_symlinked_objects_refused(env, checkout):
    objs = checkout / ".git/objects"
    objs.rename(checkout / "objs")
    objs.symlink_to(checkout / "objs")
    with pytest.raises(Refused, match="real directory"):
        call(env, "fetch", repo="me/app", path=str(checkout))


def test_shallow_refused(env, checkout):
    (checkout / ".git/shallow").write_text("")
    with pytest.raises(Refused, match="shallow"):
        call(env, "fetch", repo="me/app", path=str(checkout))


# --- the shadow: repo config and hooks never reach the gate's git --------------------------


def test_hooks_never_run(env, checkout):
    seed = env.tmp / "seed"
    commit(seed, "b.txt")
    git("push", "-q", env.url, "main", cwd=seed)
    git("switch", "-q", "-c", "topic", cwd=checkout)
    commit(checkout, "c.txt")
    marker = env.tmp / "hook-ran"
    for hook in ("reference-transaction", "pre-push", "post-checkout", "post-merge"):
        h = checkout / ".git/hooks" / hook
        h.write_text(f"#!/bin/sh\ntouch {marker}\n")
        h.chmod(0o755)
    call(env, "fetch", repo="me/app", path=str(checkout))
    call(env, "push", repo="me/app", path=str(checkout), branch="topic")
    assert git("rev-parse", "refs/heads/topic", cwd=env.bare)
    assert not marker.exists()


def test_shadow_ignores_repo_config_even_if_check_is_bypassed(env, checkout, monkeypatch):
    """Simulate the race the shadow exists for: config passes the check, then is swapped."""
    marker = env.tmp / "fsmonitor-ran"
    real_validate = env.gate.git.validate

    def validate_then_swap(*a, **kw):
        info = real_validate(*a, **kw)
        git("config", "core.fsmonitor", f"touch {marker}; false", cwd=checkout)
        git("config", f"url.file://{env.tmp}/nowhere/.insteadOf", "file://", cwd=checkout)
        return info

    monkeypatch.setattr(env.gate.git, "validate", validate_then_swap)
    out = call(env, "fetch", repo="me/app", path=str(checkout))
    assert "up to date" in out or out
    assert not marker.exists()


def _new_upstream_commit(env):
    seed = env.tmp / "seed"
    sha = commit(seed, "upstream.txt")
    git("push", "-q", env.url, "main", cwd=seed)
    return sha


def test_fetch_never_writes_through_symlinks_inside_refs(env, checkout):
    _new_upstream_commit(env)
    evil = env.tmp / "evil"
    evil.mkdir()
    remotes = checkout / ".git/refs/remotes"
    if remotes.exists():
        shutil.rmtree(remotes)
    remotes.symlink_to(evil)
    with pytest.raises(Refused, match="Could not update"):
        call(env, "fetch", repo="me/app", path=str(checkout))
    assert list(evil.iterdir()) == []


def test_fetch_never_writes_through_symlinked_pack_dir(env, checkout):
    _new_upstream_commit(env)
    evil = env.tmp / "evil"
    evil.mkdir()
    pack = checkout / ".git/objects/pack"
    for f in pack.iterdir():
        f.rename(checkout / ".git/objects" / f.name)
    pack.rmdir()
    pack.symlink_to(evil)
    with pytest.raises(Refused, match="fetched objects"):
        call(env, "fetch", repo="me/app", path=str(checkout))
    assert list(evil.iterdir()) == []


def test_objects_swapped_after_validation_refused(env, checkout, monkeypatch):
    real_validate = env.gate.git.validate

    def validate_then_swap(*a, **kw):
        info = real_validate(*a, **kw)
        objs = checkout / ".git/objects"
        objs.rename(checkout / "objs")
        objs.symlink_to(checkout / "objs")
        return info

    monkeypatch.setattr(env.gate.git, "validate", validate_then_swap)
    with pytest.raises(Refused, match="real directory"):
        call(env, "fetch", repo="me/app", path=str(checkout))


def test_fetched_objects_and_refs_land_in_agent_repo(env, checkout):
    sha = _new_upstream_commit(env)
    call(env, "fetch", repo="me/app", path=str(checkout))
    git("cat-file", "-e", f"{sha}^{{commit}}", cwd=checkout)
    git("fsck", "--no-progress", cwd=checkout)
    assert git("rev-parse", "refs/remotes/origin/main", cwd=checkout) == sha
    git("merge", "-q", "--ff-only", "origin/main", cwd=checkout)
    assert (checkout / "upstream.txt").exists()


def test_replace_refs_ignored(env, checkout):
    """refs/replace could make a workflow commit look harmless; the gate disables replacements."""
    git("switch", "-q", "-c", "sneaky", cwd=checkout)
    bad = commit(checkout, ".github/workflows/ci.yml", "on: push")
    git("switch", "-q", "main", cwd=checkout)
    git("switch", "-q", "-c", "decoy", cwd=checkout)
    good = commit(checkout, "ok.txt")
    git("replace", bad, good, cwd=checkout)
    with pytest.raises(Refused, match="workflows"):
        call(env, "push", repo="me/app", path=str(checkout), branch="sneaky")


# --- fetch ---------------------------------------------------------------------------------


def test_fetch_updates_tracking_and_reports_stale(env, checkout):
    seed = env.tmp / "seed"
    new = commit(seed, "b.txt")
    git("push", "-q", env.url, "main", "main:refs/heads/gone", cwd=seed)
    call(env, "fetch", repo="me/app", path=str(checkout))
    assert git("rev-parse", "origin/main", cwd=checkout) == new
    git("update-ref", "-d", "refs/heads/gone", cwd=env.bare)
    out = call(env, "fetch", repo="me/app", path=str(checkout))
    assert "branch -dr origin/gone" in out
    assert not (env.state / "shadow").exists() or not list((env.state / "shadow").iterdir())


def test_read_only_repo_can_fetch_but_not_push(env, tmp_path):
    (env.remote / "other").mkdir()
    git("clone", "-q", "--bare", str(env.tmp / "seed"), str(env.remote / "other/lib.git"), cwd=tmp_path)
    call(env, "clone", repo="other/lib", path=str(env.ws / "lib"))
    call(env, "fetch", repo="other/lib", path=str(env.ws / "lib"))
    with pytest.raises(Refused, match="read-only"):
        call(env, "push", repo="other/lib", path=str(env.ws / "lib"), branch="x")


# --- push ----------------------------------------------------------------------------------


def test_push_new_branch(env, checkout):
    git("switch", "-q", "-c", "feature/a", cwd=checkout)
    sha = commit(checkout, "a.txt")
    out = call(env, "push", repo="me/app", path=str(checkout), branch="feature/a")
    assert "Pushed 1 commit" in out
    assert git("rev-parse", "refs/heads/feature/a", cwd=env.bare) == sha


def test_push_fast_forward_and_up_to_date(env, checkout):
    git("switch", "-q", "-c", "f", cwd=checkout)
    commit(checkout, "a.txt")
    call(env, "push", repo="me/app", path=str(checkout), branch="f")
    sha = commit(checkout, "b.txt")
    call(env, "push", repo="me/app", path=str(checkout), branch="f")
    assert git("rev-parse", "refs/heads/f", cwd=env.bare) == sha
    assert "already at" in call(env, "push", repo="me/app", path=str(checkout), branch="f")


def test_push_refuses_non_fast_forward(env, checkout):
    git("switch", "-q", "-c", "f", cwd=checkout)
    commit(checkout, "a.txt")
    call(env, "push", repo="me/app", path=str(checkout), branch="f")
    git("reset", "-q", "--hard", "HEAD~1", cwd=checkout)
    commit(checkout, "other.txt")
    with pytest.raises(Refused, match="never force-pushes"):
        call(env, "push", repo="me/app", path=str(checkout), branch="f")


@pytest.mark.parametrize("branch", ["main", "release/1.0"])
def test_push_refuses_protected_patterns(env, checkout, branch):
    git("switch", "-q", "-C", branch, cwd=checkout)
    commit(checkout, "a.txt")
    with pytest.raises(Refused, match="default branch|protected"):
        call(env, "push", repo="me/app", path=str(checkout), branch=branch)


def test_push_refuses_default_branch_even_if_not_in_rules(env, checkout):
    git("symbolic-ref", "HEAD", "refs/heads/trunk", cwd=env.bare)
    git("switch", "-q", "-c", "trunk", cwd=checkout)
    with pytest.raises(Refused, match="default branch"):
        call(env, "push", repo="me/app", path=str(checkout), branch="trunk")


def test_push_refuses_github_protected_and_rulesets(env, checkout):
    git("switch", "-q", "-c", "guarded", cwd=checkout)
    commit(checkout, "a.txt")
    git("push", "-q", env.url, "guarded", cwd=checkout)
    env.api.protected.add("guarded")
    with pytest.raises(Refused, match="protected on GitHub"):
        call(env, "push", repo="me/app", path=str(checkout), branch="guarded")
    env.api.protected.clear()
    env.api.rules["guarded"] = {"pull_request"}
    with pytest.raises(Refused, match="ruleset"):
        call(env, "push", repo="me/app", path=str(checkout), branch="guarded")


def test_push_refuses_workflow_commits_with_handover(env, checkout):
    git("switch", "-q", "-c", "ci", cwd=checkout)
    commit(checkout, "a.txt")
    commit(checkout, ".github/workflows/ci.yml", "on: push")
    commit(checkout, ".github/workflows/ci.yml", "on: [push]")
    with pytest.raises(Refused) as e:
        call(env, "push", repo="me/app", path=str(checkout), branch="ci")
    msg = str(e.value)
    assert "git -c core.hooksPath=/dev/null push git@github.com:me/app.git ci" in msg
    assert git("for-each-ref", "refs/heads/ci", cwd=env.bare) == ""


def test_push_refuses_deleting_workflow_and_renames(env, checkout):
    seed = env.tmp / "seed"
    commit(seed, ".github/workflows/ci.yml", "on: push")
    git("push", "-q", env.url, "main", cwd=seed)
    call(env, "fetch", repo="me/app", path=str(checkout))
    git("switch", "-q", "-c", "mv", "origin/main", cwd=checkout)
    git("mv", ".github/workflows/ci.yml", "ci.yml", cwd=checkout)
    git("commit", "-q", "-m", "move", cwd=checkout)
    with pytest.raises(Refused, match="workflows"):
        call(env, "push", repo="me/app", path=str(checkout), branch="mv")


def test_clean_merge_of_main_with_workflow_change_passes(env, checkout):
    git("switch", "-q", "-c", "topic", cwd=checkout)
    commit(checkout, "topic.txt")
    seed = env.tmp / "seed"
    commit(seed, ".github/workflows/ci.yml", "on: push")
    git("push", "-q", env.url, "main", cwd=seed)
    call(env, "fetch", repo="me/app", path=str(checkout))
    git("merge", "-q", "--no-edit", "origin/main", cwd=checkout)
    assert "Pushed" in call(env, "push", repo="me/app", path=str(checkout), branch="topic")


def test_evil_merge_editing_workflow_refused(env, checkout):
    seed = env.tmp / "seed"
    commit(seed, ".github/workflows/ci.yml", "on: push")
    git("push", "-q", env.url, "main", cwd=seed)
    call(env, "fetch", repo="me/app", path=str(checkout))
    git("switch", "-q", "-c", "topic", "main", cwd=checkout)
    commit(checkout, "topic.txt")
    git("merge", "-q", "--no-commit", "origin/main", cwd=checkout)
    (checkout / ".github/workflows/ci.yml").write_text("on: everything")
    git("add", ".", cwd=checkout)
    git("commit", "-q", "--no-edit", cwd=checkout)
    with pytest.raises(Refused, match="workflows"):
        call(env, "push", repo="me/app", path=str(checkout), branch="topic")


def test_merge_rolling_workflow_back_to_older_version_refused(env, checkout):
    """Dense-combined diffs hide a merge that keeps one parent's older workflow; the tree check must not."""
    seed = env.tmp / "seed"
    old = commit(seed, ".github/workflows/ci.yml", "v1")
    commit(seed, ".github/workflows/ci.yml", "v2")
    git("push", "-q", env.url, "main", cwd=seed)
    call(env, "fetch", repo="me/app", path=str(checkout))
    git("switch", "-q", "-c", "side", old, cwd=checkout)
    commit(checkout, "side.txt")
    git("switch", "-q", "-c", "topic", "origin/main", cwd=checkout)
    commit(checkout, "topic.txt")
    git("merge", "-q", "--no-commit", "side", cwd=checkout)
    git("checkout", "side", "--", ".github/workflows/ci.yml", cwd=checkout)
    git("commit", "-q", "--no-edit", cwd=checkout)
    assert (checkout / ".github/workflows/ci.yml").read_text() == "v1"
    with pytest.raises(Refused, match="workflows"):
        call(env, "push", repo="me/app", path=str(checkout), branch="topic")


def test_stale_fork_without_workflow_changes_passes(env, checkout):
    """Main gaining a workflow change after the fork is not this branch's change."""
    git("switch", "-q", "-c", "topic", cwd=checkout)
    commit(checkout, "topic.txt")
    seed = env.tmp / "seed"
    commit(seed, ".github/workflows/ci.yml", "on: push")
    git("push", "-q", env.url, "main", cwd=seed)
    assert "Pushed" in call(env, "push", repo="me/app", path=str(checkout), branch="topic")


def test_push_never_sends_tags(env, checkout):
    git("switch", "-q", "-c", "t", cwd=checkout)
    commit(checkout, "a.txt")
    git("tag", "v9", cwd=checkout)
    git("tag", "-a", "v10", "-m", "x", cwd=checkout)
    call(env, "push", repo="me/app", path=str(checkout), branch="t")
    assert git("tag", cwd=env.bare) == ""


def test_push_rejects_bad_branch_names(env, checkout):
    for bad in ("--force", "a..b", "HEAD", "x:y", ""):
        with pytest.raises(Refused):
            call(env, "push", repo="me/app", path=str(checkout), branch=bad)


def test_push_missing_local_branch(env, checkout):
    with pytest.raises(Refused, match="No local branch"):
        call(env, "push", repo="me/app", path=str(checkout), branch="nope")


# --- prune ---------------------------------------------------------------------------------


def _pushed_branch(env, checkout, name="done"):
    git("switch", "-q", "-c", name, cwd=checkout)
    sha = commit(checkout, f"{name}.txt")
    call(env, "push", repo="me/app", path=str(checkout), branch=name)
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


# --- config, token, audit ------------------------------------------------------------------


def test_token_must_be_private(env, checkout):
    (env.cfg / "token").chmod(0o644)
    with pytest.raises(Refused, match="0600"):
        call(env, "fetch", repo="me/app", path=str(checkout))


def test_workspace_may_not_overlap_gate_dirs(env, checkout):
    (env.cfg / "rules.toml").write_text(f'workspaces = ["{env.tmp}"]\n[repos]\n"me/app" = "write"\n')
    with pytest.raises(Refused, match="overlaps"):
        call(env, "fetch", repo="me/app", path=str(checkout))


def test_audit_log_has_no_token(env, checkout):
    call(env, "fetch", repo="me/app", path=str(checkout))
    with pytest.raises(Refused):
        call(env, "fetch", repo="me/app", path=TOKEN)
    lines = (env.state / "audit.log").read_text().splitlines()
    assert [json.loads(line)["outcome"] for line in lines] == ["ok", "refused"]
    assert TOKEN not in "".join(lines)
