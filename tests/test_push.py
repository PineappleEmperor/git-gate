import io
import json
import os
import re
import threading
import time
import urllib.error

import pytest

import git_gate.gate as gate_module
from conftest import TOKEN, call, commit, git, make_bundle, push
from git_gate import Refused
from git_gate.gate import Gate
from git_gate.github import GitHub
from git_gate.handover import Handovers


def remote_ref(env, branch: str) -> str:
    return git("for-each-ref", "--format=%(objectname)", f"refs/heads/{branch}", cwd=env.bare)


def remote_refs(env) -> str:
    return git("for-each-ref", "--format=%(objectname) %(refname)", cwd=env.bare)


def mirror_ref(env, ref: str) -> str:
    return git(f"--git-dir={env.state / 'mirrors/me/app.git'}", "for-each-ref", "--format=%(objectname)", ref)


def main_sha(env) -> str:
    return remote_ref(env, "main")


def staging_empty(env) -> bool:
    return list((env.state / "staging").iterdir()) == []


# --- pushing a feature branch --------------------------------------------------------------


def test_push_feature_branch(env, checkout):
    git("switch", "-q", "-c", "feat", cwd=checkout)
    tip = commit(checkout, "a.txt")
    out = push(env, checkout, "feat")
    assert "Pushed 1 commit" in out
    assert "https://github.com/me/app/compare/main...feat" in out
    assert "This push deploys." not in out
    assert remote_ref(env, "feat") == tip
    assert staging_empty(env)

    env.api.open_pulls["feat"] = "https://github.com/me/app/pull/7"
    tip = commit(checkout, "b.txt")
    out = push(env, checkout, "feat")
    assert "https://github.com/me/app/pull/7" in out
    assert "/compare/" not in out
    assert remote_ref(env, "feat") == tip

    (env.cfg / "rules.toml").write_text(
        f'workspaces = ["{env.ws}"]\n[repos]\n"me/app" = {{ access = "write", push_deploys = true }}\n'
    )
    tip = commit(checkout, "c.txt")
    out = push(env, checkout, "feat")
    assert "This push deploys." in out.splitlines()
    assert remote_ref(env, "feat") == tip


def test_push_fast_forward_and_up_to_date(env, checkout):
    git("switch", "-q", "-c", "f", cwd=checkout)
    commit(checkout, "a.txt")
    push(env, checkout, "f")
    sha = commit(checkout, "b.txt")
    push(env, checkout, "f")
    assert remote_ref(env, "f") == sha
    out = env.ws / "again.bundle"
    make_bundle(checkout, out, "f", "origin/main")
    assert "already at" in call(env, "push", repo="me/app", branch="f", bundle=str(out))


def test_push_rejects_bad_branch_names(env, checkout):
    git("switch", "-q", "-c", "ok", cwd=checkout)
    commit(checkout, "a.txt")
    out = make_bundle(checkout, env.ws / "ok.bundle", "ok")
    for bad in ("--force", "a..b", "HEAD", "x:y", ""):
        with pytest.raises(Refused):
            call(env, "push", repo="me/app", branch=bad, bundle=str(out))
    assert remote_ref(env, "ok") == ""


def test_push_bundle_must_hold_only_the_branch(env, checkout):
    git("switch", "-q", "-c", "t", cwd=checkout)
    commit(checkout, "a.txt")
    git("tag", "v9", cwd=checkout)
    out = env.ws / "t.bundle"
    git("bundle", "create", "-q", str(out), "t", "v9", "--not", "--remotes=origin", cwd=checkout)
    with pytest.raises(Refused, match="exactly one ref"):
        call(env, "push", repo="me/app", branch="t", bundle=str(out))
    with pytest.raises(Refused, match="exactly one ref"):
        call(env, "push", repo="me/app", branch="other", bundle=str(make_bundle(checkout, env.ws / "u.bundle", "t")))
    assert git("tag", cwd=env.bare) == ""
    assert remote_ref(env, "t") == ""


def test_read_only_repo_cannot_push(env, checkout):
    git("switch", "-q", "-c", "x", cwd=checkout)
    commit(checkout, "a.txt")
    with pytest.raises(Refused, match="read-only"):
        push(env, checkout, "x", repo="other/lib")


def test_clean_merge_of_main_with_workflow_change_passes(env, checkout):
    git("switch", "-q", "-c", "topic", cwd=checkout)
    commit(checkout, "topic.txt")
    seed = env.tmp / "seed"
    commit(seed, ".github/workflows/ci.yml", "on: push")
    git("push", "-q", env.url, "main", cwd=seed)
    git("fetch", "-q", "origin", cwd=checkout)
    git("merge", "-q", "--no-edit", "origin/main", cwd=checkout)
    assert "Pushed" in push(env, checkout, "topic")


def test_stale_fork_without_workflow_changes_passes(env, checkout):
    """Main gaining a workflow change after the fork is not this branch's change."""
    git("switch", "-q", "-c", "topic", cwd=checkout)
    commit(checkout, "topic.txt")
    seed = env.tmp / "seed"
    commit(seed, ".github/workflows/ci.yml", "on: push")
    git("push", "-q", env.url, "main", cwd=seed)
    assert "Pushed" in push(env, checkout, "topic")


# --- refusals become handovers -------------------------------------------------------------


def _setup_default(env, checkout):
    return "main", "default branch", main_sha(env), None


def _setup_pattern(env, checkout):
    git("switch", "-q", "-c", "release/1", cwd=checkout)
    return "release/1", "protected pattern", main_sha(env), None


def _setup_github_protected(env, checkout):
    git("switch", "-q", "-c", "guarded", cwd=checkout)
    remote = commit(checkout, "g.txt")
    git("push", "-q", "origin", "guarded", cwd=checkout)
    env.api.protected.add("guarded")
    return "guarded", "protected on GitHub", remote, None


def _setup_ruleset(env, checkout):
    git("switch", "-q", "-c", "ruled", cwd=checkout)
    env.api.rules["ruled"] = {"pull_request", "creation"}
    return "ruled", "ruleset rules: pull_request", main_sha(env), None


def _setup_non_fast_forward(env, checkout):
    git("switch", "-q", "-c", "f", cwd=checkout)
    remote = commit(checkout, "a.txt")
    git("push", "-q", "origin", "f", cwd=checkout)
    git("reset", "-q", "--hard", "HEAD~1", cwd=checkout)
    return "f", "not a fast-forward", remote, remote


def _path_case(path):
    def setup(env, checkout):
        git("switch", "-q", "-c", "paths", cwd=checkout)
        commit(checkout, "ok.txt")
        commit(checkout, path, "changed")
        return "paths", "handover path", main_sha(env), None

    return setup


def _setup_extra_path(env, checkout):
    (env.cfg / "rules.toml").write_text(
        f'workspaces = ["{env.ws}"]\n[repos]\n"me/app" = {{ access = "write", handover_paths = ["skills/**"] }}\n'
    )
    git("switch", "-q", "-c", "skill", cwd=checkout)
    commit(checkout, "skills/x/SKILL.md", "new")
    return "skill", "skills", main_sha(env), None


@pytest.mark.parametrize(
    "setup",
    [
        _setup_default,
        _setup_pattern,
        _setup_github_protected,
        _setup_ruleset,
        _setup_non_fast_forward,
        _path_case(".github/workflows/ci.yml"),
        _path_case(".githooks/pre-commit"),
        _path_case(".pre-commit-config.yaml"),
        _setup_extra_path,
    ],
    ids=["default", "pattern", "github", "ruleset", "rewrite", "workflows", "githooks", "pre-commit", "extra"],
)
def test_push_refusals_create_handovers(env, checkout, setup):
    branch, reason, base, lease = setup(env, checkout)
    tip = commit(checkout, "tip.txt")
    before = remote_refs(env)
    with pytest.raises(Refused) as e:
        push(env, checkout, branch)
    msg = str(e.value)
    assert reason in msg
    found = re.findall(r"gitgatepush (\d+)", msg)
    assert len(found) == 1
    h = Handovers(env.state).get(int(found[0]))
    assert (h.repo, h.branch, h.sha, h.base, h.lease) == ("me/app", branch, tip, base, lease)
    assert mirror_ref(env, f"refs/gate/handover/{h.id}") == tip
    assert remote_refs(env) == before
    assert staging_empty(env)
    audit = [json.loads(line) for line in (env.state / "audit.log").read_text().splitlines()]
    assert audit[-1]["outcome"] == "handover"


def test_new_branch_handover_records_fork_point(env, checkout):
    fork = main_sha(env)
    seed = env.tmp / "seed"
    commit(seed, "later.txt")
    git("push", "-q", env.url, "main", cwd=seed)
    git("switch", "-q", "-c", "ci", cwd=checkout)
    tip = commit(checkout, ".github/workflows/ci.yml", "on: push")
    with pytest.raises(Refused, match=r"gitgatepush \d+") as e:
        push(env, checkout, "ci")
    h = Handovers(env.state).get(int(re.findall(r"gitgatepush (\d+)", str(e.value))[0]))
    assert (h.sha, h.base, h.lease) == (tip, fork, None)


def test_private_free_repo_push(env, checkout):
    """Free-plan private repos answer the rules endpoint with 403 "Upgrade to GitHub Pro": no rules."""

    def opener(message: str):
        class Opener:
            def open(self, req, timeout):
                body = io.BytesIO(json.dumps({"message": message}).encode())
                raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, body)

        return Opener()

    real = GitHub(TOKEN)
    env.api.rule_types = real.rule_types
    real._opener = opener("Upgrade to GitHub Pro or make this repository public to enable this feature.")
    git("switch", "-q", "-c", "feat", cwd=checkout)
    tip = commit(checkout, "a.txt")
    assert "Pushed" in push(env, checkout, "feat")
    assert remote_ref(env, "feat") == tip

    real._opener = opener("Resource not accessible by personal access token")
    commit(checkout, "b.txt")
    with pytest.raises(Refused, match="403"):
        push(env, checkout, "feat")
    assert remote_ref(env, "feat") == tip
    assert Handovers(env.state).open() == []


# --- the bundle path -----------------------------------------------------------------------


def _bundle_in(env, checkout, name="b.bundle"):
    git("switch", "-q", "-c", "feat", cwd=checkout)
    commit(checkout, "a.txt")
    return make_bundle(checkout, env.ws / name, "feat")


@pytest.mark.parametrize("kind", ["fifo", "symlink", "directory", "outside", "too-big", "missing", "relative"])
def test_push_bundle_paths(env, checkout, monkeypatch, kind):
    real = _bundle_in(env, checkout)
    path = env.ws / "agent.bundle"
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "symlink":
        path.symlink_to(real)
    elif kind == "directory":
        path.mkdir()
    elif kind == "outside":
        path = env.tmp / "outside.bundle"
        real.rename(path)
    elif kind == "too-big":
        monkeypatch.setattr(gate_module, "MAX_BUNDLE", 10)
        path = real
    elif kind == "relative":
        path = "b.bundle"
    start = time.monotonic()
    with pytest.raises(Refused):
        call(env, "push", repo="me/app", branch="feat", bundle=str(path))
    assert time.monotonic() - start < 2
    assert remote_ref(env, "feat") == ""
    assert staging_empty(env)


def test_concurrent_pushes(env, checkout):
    """Two conversations push to one repo at once: each in its own process, sharing only the state dir."""
    bundles = {}
    for name in ("a", "b"):
        git("switch", "-q", "-c", name, "main", cwd=checkout)
        commit(checkout, f"{name}.txt")
        bundles[name] = make_bundle(checkout, env.ws / f"{name}.bundle", name)
    results, errors = {}, []

    def run(name):
        gate = Gate(env.cfg, env.state, base_url=f"file://{env.remote}", github=env.api)
        try:
            results[name] = gate.call("push", gate.push, repo="me/app", branch=name, bundle=str(bundles[name]))
        except Exception as e:  # noqa: BLE001 - reported below
            errors.append(e)

    threads = [threading.Thread(target=run, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert all("Pushed" in r for r in results.values())
    assert remote_ref(env, "a") and remote_ref(env, "b")


def test_api_failure_mid_push(env, checkout):
    git("switch", "-q", "-c", "feat", cwd=checkout)
    commit(checkout, "a.txt")
    before = remote_refs(env)
    env.api.fail_branch = Refused("GitHub GET repos/me/app/branches/feat returned 502.")
    with pytest.raises(Refused, match="502"):
        push(env, checkout, "feat")
    assert Handovers(env.state).open() == []
    assert remote_refs(env) == before
    assert staging_empty(env)
