import json
import os
import re
import shutil
from pathlib import Path

import pytest

from conftest import TOKEN, call, commit, git, make_bundle
from git_gate import Refused
from git_gate.config import OFF_SWITCH_MESSAGE

REFSPEC = "+refs/heads/*:refs/remotes/origin/*"


def upstream(env, path: str = "upstream.txt", *extra: str) -> str:
    seed = env.tmp / "seed"
    sha = commit(seed, path)
    git("push", "-q", env.url, "main", *extra, cwd=seed)
    return sha


def heads(env) -> dict[str, str]:
    out = git("for-each-ref", "--format=%(refname:lstrip=2) %(objectname)", "refs/heads/", cwd=env.bare)
    return dict(line.split(" ") for line in out.splitlines())


# --- fetch ---------------------------------------------------------------------------------


def test_fetch_writes_bundle(env, checkout):
    new = upstream(env, "upstream.txt", "main:refs/heads/topic")
    out_path = env.ws / "fetch.bundle"
    out = call(env, "fetch", repo="me/app", bundle=str(out_path))
    for name, sha in heads(env).items():
        assert f"{name} {sha}" in out.splitlines()
    assert REFSPEC in out
    git("fetch", "-q", str(out_path), REFSPEC, cwd=checkout)
    assert git("rev-parse", "origin/main", cwd=checkout) == new
    assert git("rev-parse", "origin/topic", cwd=checkout) == new

    out_path.unlink()
    have = git("for-each-ref", "--format=%(objectname)", "refs/remotes/origin/", cwd=checkout).split()
    out = call(env, "fetch", repo="me/app", bundle=str(out_path), have=have)
    assert "Up to date" in out
    assert not out_path.exists()

    newer = upstream(env, "newer.txt")
    out = call(env, "fetch", repo="me/app", bundle=str(out_path), have=have)
    git("fetch", "-q", str(out_path), REFSPEC, cwd=checkout)
    assert git("rev-parse", "origin/main", cwd=checkout) == newer


def test_fetch_replaces_an_existing_bundle_file(env, checkout):
    new = upstream(env)
    out_path = env.ws / "fetch.bundle"
    out_path.write_text("old")
    call(env, "fetch", repo="me/app", bundle=str(out_path))
    git("fetch", "-q", str(out_path), REFSPEC, cwd=checkout)
    assert git("rev-parse", "origin/main", cwd=checkout) == new


@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo", "outside", "relative", "parent-symlink"])
def test_fetch_output_path_refused(env, kind):
    target = env.tmp / "target"
    path = env.ws / "fetch.bundle"
    if kind == "symlink":
        path.symlink_to(target)
    elif kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "outside":
        path = env.tmp / "fetch.bundle"
    elif kind == "relative":
        path = "fetch.bundle"
    elif kind == "parent-symlink":
        target.mkdir()
        (env.ws / "link").symlink_to(target)
        path = env.ws / "link" / "fetch.bundle"
    with pytest.raises(Refused):
        call(env, "fetch", repo="me/app", bundle=str(path))
    assert not (env.tmp / "fetch.bundle").exists()
    assert not target.exists() or target.is_dir() and list(target.iterdir()) == []


@pytest.mark.parametrize(
    "have", ["abc", {"a": 1}, [1], ["a" * 40, None], ["0" * 40] * 1001], ids=["str", "dict", "int", "none", "many"]
)
def test_fetch_have_checked_before_any_git(env, monkeypatch, have):
    def boom(*a, **kw):
        raise AssertionError("git ran")

    monkeypatch.setattr(env.gate.git, "run", boom)
    with pytest.raises(Refused, match="have"):
        call(env, "fetch", repo="me/app", bundle=str(env.ws / "f.bundle"), have=have)


def test_fetch_empty_remote(env):
    git("init", "-q", "--bare", str(env.remote / "me/empty.git"))
    (env.cfg / "rules.toml").write_text(f'workspaces = ["{env.ws}"]\n[repos]\n"me/empty" = "read"\n')
    out = call(env, "fetch", repo="me/empty", bundle=str(env.ws / "f.bundle"))
    assert "no branches" in out
    assert not (env.ws / "f.bundle").exists()


def test_read_only_repo_can_fetch(env, tmp_path):
    (env.remote / "other").mkdir()
    git("clone", "-q", "--bare", str(env.tmp / "seed"), str(env.remote / "other/lib.git"), cwd=tmp_path)
    call(env, "fetch", repo="other/lib", bundle=str(env.ws / "lib.bundle"))
    assert (env.ws / "lib.bundle").is_file()


# --- the agent's repository is never read --------------------------------------------------


def snapshot(root: Path) -> dict[str, tuple]:
    found = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            st = p.lstat()
            if p.is_symlink():
                found[str(p)] = ("link", os.readlink(p))
            elif p.is_file():
                found[str(p)] = ("file", st.st_mode, st.st_mtime_ns, p.read_bytes())
            else:
                found[str(p)] = ("dir", st.st_mode)
    return found


def test_agent_repo_never_touched(env, checkout):
    git("switch", "-q", "-c", "topic", cwd=checkout)
    commit(checkout, "c.txt")
    bundle = make_bundle(checkout, env.ws / "topic.bundle", "topic")
    upstream(env)

    marker = env.tmp / "ran"
    for hook in ("reference-transaction", "pre-push", "post-checkout", "post-merge", "pre-auto-gc"):
        h = checkout / ".git/hooks" / hook
        h.write_text(f"#!/bin/sh\ntouch {marker}-{hook}\n")
        h.chmod(0o755)
    git("config", "core.fsmonitor", f"touch {marker}-fsmonitor; false", cwd=checkout)
    git("config", "core.sshCommand", f"touch {marker}-ssh", cwd=checkout)
    secret = env.tmp / "secret"
    secret.mkdir()
    git("init", "-q", cwd=secret)
    commit(secret, "key.txt", "TOP SECRET")
    (checkout / ".git/objects/info").mkdir(exist_ok=True)
    (checkout / ".git/objects/info/alternates").write_text(f"{secret}/.git/objects\n")
    evil = env.tmp / "evil"
    evil.mkdir()
    shutil.rmtree(checkout / ".git/refs/remotes")
    (checkout / ".git/refs/remotes").symlink_to(evil)

    before = snapshot(checkout)
    assert "Pushed" in call(env, "push", repo="me/app", branch="topic", bundle=str(bundle))
    call(env, "fetch", repo="me/app", bundle=str(env.ws / "fetch.bundle"))
    assert snapshot(checkout) == before
    assert list(env.tmp.glob("ran*")) == []
    assert list(evil.iterdir()) == []


# --- call plumbing: off switch, rate limit, token, audit -----------------------------------


def test_off_switch_and_rate_limit(env, checkout):
    (env.cfg / "disabled").touch()
    calls = {
        "clone": {"repo": "me/app", "path": str(env.ws / "new")},
        "fetch": {"repo": "me/app", "bundle": str(env.ws / "f.bundle")},
        "push": {"repo": "me/app", "branch": "x", "bundle": str(env.ws / "x.bundle")},
        "prune": {"repo": "me/app", "branch": "x"},
        "gh": {"repo": "me/app", "args": ["repo", "view"]},
    }
    for tool, kwargs in calls.items():
        with pytest.raises(Refused, match=f"^{re.escape(OFF_SWITCH_MESSAGE)}$"):
            call(env, tool, **kwargs)
    (env.cfg / "disabled").unlink()

    def ok(rules, token, **kw):
        return "ok"

    now = [1_000_000.0]
    env.gate.clock = lambda: now[0]
    for _ in range(60):
        assert env.gate.call("prune", ok, repo="me/app") == "ok"
    with pytest.raises(Refused, match="60 calls"):
        env.gate.call("prune", ok, repo="me/app")
    with pytest.raises(Refused, match="60 calls"):
        env.gate.call("prune", ok, repo="ME/App")
    assert env.gate.call("prune", ok, repo="other/lib") == "ok"
    now[0] += 61
    assert env.gate.call("prune", ok, repo="me/app") == "ok"


def test_token_must_be_private(env):
    (env.cfg / "token").chmod(0o644)
    with pytest.raises(Refused, match="0600"):
        call(env, "fetch", repo="me/app", bundle=str(env.ws / "f.bundle"))


def test_workspace_may_not_overlap_gate_dirs(env):
    (env.cfg / "rules.toml").write_text(f'workspaces = ["{env.tmp}"]\n[repos]\n"me/app" = "write"\n')
    with pytest.raises(Refused, match="overlaps"):
        call(env, "fetch", repo="me/app", bundle=str(env.ws / "f.bundle"))


def test_audit_log_has_no_token(env):
    call(env, "fetch", repo="me/app", bundle=str(env.ws / "f.bundle"))
    with pytest.raises(Refused):
        call(env, "fetch", repo="me/app", bundle=TOKEN)
    lines = (env.state / "audit.log").read_text().splitlines()
    assert [json.loads(line)["outcome"] for line in lines] == ["ok", "refused"]
    assert TOKEN not in "".join(lines)
