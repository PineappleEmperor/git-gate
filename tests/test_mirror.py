import os
import subprocess
from pathlib import Path

import pytest

from conftest import TOKEN, commit, git
from git_gate import Refused
from git_gate.config import DEFAULT_HANDOVER_PATHS, canonical_pattern, load_rules
from git_gate.gitops import Git
from git_gate.mirror import Mirror, protected, reaches

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

    none = env.tmp / "none.bundle"
    assert m.export_bundle(REPO, none, [m2, dev], rules) is False
    assert not none.exists()


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
    assert m.export_bundle(REPO, env.tmp / "ok.bundle", ["0" * 40] * 999 + [main], rules) is False


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


# --- touched -------------------------------------------------------------------------------


def _touched(env, m, rules, checkout, branch, patterns=DEFAULT_HANDOVER_PATHS):
    """Bundle `branch` from the checkout, import it, and run the checks as push will."""
    heads = m.sync(REPO, TOKEN, rules)
    sha = m.import_bundle(REPO, bundle(checkout, env.state / "tmp" / f"{branch}.bundle", branch), branch, rules)
    return sha, m.touched(REPO, sha, heads.get(branch), heads.get("main"), patterns, rules)


def user_branch(env, name: str, path: str, content: str) -> str:
    """The user pushes `name` to GitHub: main plus one commit writing `path`."""
    seed = env.tmp / "seed"
    git("switch", "-q", "-c", name, "main", cwd=seed)
    sha = commit(seed, path, content)
    git("push", "-q", env.url, name, cwd=seed)
    git("switch", "-q", "main", cwd=seed)
    return sha


def test_touched_names_workflow_commits(env, m, rules, checkout):
    git("switch", "-q", "-c", "ci", cwd=checkout)
    commit(checkout, "a.txt")
    one = commit(checkout, ".github/workflows/ci.yml", "on: push")
    two = commit(checkout, ".github/workflows/ci.yml", "on: [push]")
    _, hits = _touched(env, m, rules, checkout, "ci")
    assert sorted(hits) == sorted([one, two])


def test_touched_deleting_workflow_by_rename(env, m, rules, checkout):
    seed_push(env, ".github/workflows/ci.yml", "on: push")
    git("fetch", "-q", "origin", cwd=checkout)
    git("switch", "-q", "-c", "mv", "origin/main", cwd=checkout)
    git("mv", ".github/workflows/ci.yml", "ci.yml", cwd=checkout)
    git("commit", "-q", "-m", "move", cwd=checkout)
    sha, hits = _touched(env, m, rules, checkout, "mv")
    assert hits == [sha]


def test_touched_clean_merge_of_main_with_workflow_change_passes(env, m, rules, checkout):
    git("switch", "-q", "-c", "topic", cwd=checkout)
    commit(checkout, "topic.txt")
    seed_push(env, ".github/workflows/ci.yml", "on: push")
    git("fetch", "-q", "origin", cwd=checkout)
    git("merge", "-q", "--no-edit", "origin/main", cwd=checkout)
    assert _touched(env, m, rules, checkout, "topic")[1] == []


def test_touched_evil_merge_editing_workflow(env, m, rules, checkout):
    seed_push(env, ".github/workflows/ci.yml", "on: push")
    git("fetch", "-q", "origin", cwd=checkout)
    git("switch", "-q", "-c", "topic", "main", cwd=checkout)
    commit(checkout, "topic.txt")
    git("merge", "-q", "--no-commit", "origin/main", cwd=checkout)
    (checkout / ".github/workflows/ci.yml").write_text("on: everything")
    git("add", ".", cwd=checkout)
    git("commit", "-q", "--no-edit", cwd=checkout)
    sha, hits = _touched(env, m, rules, checkout, "topic")
    assert hits == [sha]


def test_touched_merge_rolling_workflow_back(env, m, rules, checkout):
    """Dense-combined diffs hide a merge that keeps one parent's older workflow; the tree check must not."""
    old = seed_push(env, ".github/workflows/ci.yml", "v1")
    seed_push(env, ".github/workflows/ci.yml", "v2")
    git("fetch", "-q", "origin", cwd=checkout)
    git("switch", "-q", "-c", "side", old, cwd=checkout)
    commit(checkout, "side.txt")
    git("switch", "-q", "-c", "topic", "origin/main", cwd=checkout)
    commit(checkout, "topic.txt")
    git("merge", "-q", "--no-commit", "side", cwd=checkout)
    git("checkout", "side", "--", ".github/workflows/ci.yml", cwd=checkout)
    git("commit", "-q", "--no-edit", cwd=checkout)
    assert (checkout / ".github/workflows/ci.yml").read_text() == "v1"
    sha, hits = _touched(env, m, rules, checkout, "topic")
    assert hits == [sha]


def test_touched_stale_fork_with_old_workflows(env, m, rules, checkout):
    """Main gaining a workflow fix after the fork: a new branch there would run the old workflows."""
    git("switch", "-q", "-c", "topic", cwd=checkout)
    commit(checkout, "topic.txt")
    seed_push(env, ".github/workflows/ci.yml", "on: push")
    sha, hits = _touched(env, m, rules, checkout, "topic")
    assert hits == [sha]


def test_touched_stale_fork_without_workflow_drift_passes(env, m, rules, checkout):
    git("switch", "-q", "-c", "topic", cwd=checkout)
    commit(checkout, "topic.txt")
    seed_push(env, "docs/later.md", "later")
    assert _touched(env, m, rules, checkout, "topic")[1] == []


def test_touched_existing_branch_merges_main_after_user_workflow_change_passes(env, m, rules, checkout):
    """The user's own workflow commit on main, merged into a pushed branch, is not re-checked."""
    git("switch", "-q", "-c", "topic", cwd=checkout)
    commit(checkout, "topic.txt")
    git("push", "-q", env.url, "topic", cwd=checkout)
    seed_push(env, ".github/workflows/ci.yml", "on: push")
    git("fetch", "-q", "origin", cwd=checkout)
    git("merge", "-q", "--no-edit", "origin/main", cwd=checkout)
    assert _touched(env, m, rules, checkout, "topic")[1] == []


def test_touched_existing_branch_keeping_its_old_workflows_passes(env, m, rules, checkout):
    seed_push(env, ".github/workflows/ci.yml", "v1")
    git("fetch", "-q", "origin", cwd=checkout)
    git("switch", "-q", "-c", "topic", "origin/main", cwd=checkout)
    commit(checkout, "topic.txt")
    git("push", "-q", env.url, "topic", cwd=checkout)
    seed_push(env, ".github/workflows/ci.yml", "v2")
    commit(checkout, "more.txt")
    assert _touched(env, m, rules, checkout, "topic")[1] == []


def test_touched_rewrite_with_new_protected_content(env, m, rules, checkout):
    seed_push(env, ".github/workflows/ci.yml", "v1")
    user_branch(env, "feat", ".github/workflows/ci.yml", "v2")
    git("fetch", "-q", "origin", cwd=checkout)
    git("switch", "-q", "-c", "feat", "origin/main", cwd=checkout)
    v3 = commit(checkout, ".github/workflows/ci.yml", "v3")
    assert _touched(env, m, rules, checkout, "feat")[1] == [v3]


def test_touched_rewrite_back_to_the_fork_version_is_not_protected(env, m, rules, checkout):
    seed_push(env, ".github/workflows/ci.yml", "v1")
    user_branch(env, "feat", ".github/workflows/ci.yml", "v2")
    git("fetch", "-q", "origin", cwd=checkout)
    git("switch", "-q", "-c", "feat", "origin/main", cwd=checkout)
    commit(checkout, "other.txt")
    assert _touched(env, m, rules, checkout, "feat")[1] == []


def test_touched_fast_forward_keeping_the_remotes_protected_change_passes(env, m, rules, checkout):
    seed_push(env, ".github/workflows/ci.yml", "v1")
    user_branch(env, "feat", ".github/workflows/ci.yml", "v2")
    git("fetch", "-q", "origin", cwd=checkout)
    git("switch", "-q", "-c", "feat", "origin/feat", cwd=checkout)
    commit(checkout, "other.txt")
    assert _touched(env, m, rules, checkout, "feat")[1] == []


def test_touched_against_existing_remote_branch(env, m, rules, checkout):
    git("switch", "-q", "-c", "topic", cwd=checkout)
    commit(checkout, "topic.txt")
    git("push", "-q", env.url, "topic", cwd=checkout)
    git("fetch", "-q", "origin", cwd=checkout)
    wf = commit(checkout, ".github/workflows/ci.yml")
    commit(checkout, "more.txt")
    assert _touched(env, m, rules, checkout, "topic")[1] == [wf]


def test_touched_root_commit_without_base(env, m, rules, checkout):
    git("switch", "-q", "--orphan", "fresh", cwd=checkout)
    root = commit(checkout, ".githooks/pre-commit")
    m.sync(REPO, TOKEN, rules)
    sha = m.import_bundle(REPO, bundle(checkout, env.state / "tmp" / "f.bundle", "fresh"), "fresh", rules)
    assert sha == root
    assert m.touched(REPO, sha, None, None, DEFAULT_HANDOVER_PATHS, rules) == [root]


def test_touched_ignores_replace_refs(env, m, rules, checkout):
    """refs/replace could make a workflow commit look harmless; the gate's git disables replacements."""
    git("switch", "-q", "-c", "sneaky", cwd=checkout)
    bad = commit(checkout, ".github/workflows/ci.yml", "on: push")
    git("switch", "-q", "main", cwd=checkout)
    git("switch", "-q", "-c", "decoy", cwd=checkout)
    good = commit(checkout, "ok.txt")
    git("push", "-q", env.url, "sneaky", "decoy", cwd=checkout)
    heads = m.sync(REPO, TOKEN, rules)
    mgit(m, "replace", bad, good)
    assert mgit(m, "for-each-ref", "refs/replace/") != ""
    main = heads["main"]
    assert m.new_commits(REPO, bad, main, rules) == [bad]
    assert m.touched(REPO, bad, None, main, DEFAULT_HANDOVER_PATHS, rules) == [bad]


@pytest.mark.parametrize(
    ("path", "extra", "hit"),
    [
        (".githooks/pre-commit", (), True),
        (".pre-commit-config.yaml", (), True),
        (".github/workflows/sub/x.yml", (), True),
        ("skills/x.md", (), False),
        ("skills/x.md", ("skills/**",), True),
        ("docs/.pre-commit-config.yaml", (), False),
        (".github/dependabot.yml", (), False),
        (".GITHUB/workflows/ci.yml", (), True),
        (".GitHooks/pre-commit", (), True),
        (".Pre-Commit-Config.YAML", (), True),
        ("Skills/X.md", ("skills/**",), True),
        ("skills/x.md", ("SKILLS/**",), True),
        # patterns are normalised
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
def test_touched_patterns(env, m, rules, checkout, path, extra, hit):
    git("switch", "-q", "-c", "topic", cwd=checkout)
    sha = commit(checkout, path)
    _, hits = _touched(env, m, rules, checkout, "topic", DEFAULT_HANDOVER_PATHS + canon(*extra))
    assert hits == ([sha] if hit else [])


def test_touched_pattern_directory_as_file(env, m, rules, checkout):
    """A file at the pattern's directory (`.github/workflows` itself) is a handover path too.

    A regular file `skills` cannot hold anything `skills/*.md` names, so it is not one (a symlink
    there would be: see `reaches`).
    """
    git("switch", "-q", "-c", "topic", cwd=checkout)
    sha = commit(checkout, ".github/workflows", "not a directory")
    assert _touched(env, m, rules, checkout, "topic")[1] == [sha]
    git("switch", "-q", "-c", "sk", "main", cwd=checkout)
    commit(checkout, "skills", "file")
    assert _touched(env, m, rules, checkout, "sk", canon("skills/*.md"))[1] == []


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


# --- symlinks into handover paths ----------------------------------------------------------


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
def test_touched_tip_with_symlink_into_handover_path(env, m, rules, checkout, path, target, files, edit):
    """A symlink at or above a handover path makes any edit through it a handover-path change."""
    _seed_link(env, path, target, files)
    git("fetch", "-q", "origin", cwd=checkout)
    git("switch", "-q", "-c", "topic", "origin/main", cwd=checkout)
    sha = commit(checkout, edit, "changed")
    assert _touched(env, m, rules, checkout, "topic")[1] == [sha]


def test_touched_commit_adding_dotgithub_symlink(env, m, rules, checkout):
    git("switch", "-q", "-c", "topic", cwd=checkout)
    commit(checkout, "ci/workflows/ci.yml", "on: push")
    sha = link(checkout, ".github", "ci")
    assert _touched(env, m, rules, checkout, "topic")[1] == [sha]


def test_touched_symlink_matching_extra_pattern_case_insensitively(env, m, rules, checkout):
    git("switch", "-q", "-c", "topic", cwd=checkout)
    commit(checkout, "real/x.md")
    sha = link(checkout, "Skills", "real")
    assert _touched(env, m, rules, checkout, "topic", DEFAULT_HANDOVER_PATHS + canon("skills/**"))[1] == [sha]


def test_touched_ordinary_symlinks_pass(env, m, rules, checkout):
    _seed_link(env, "docs/readme", "../README.md", {"docs/index.md": "hi"})
    git("fetch", "-q", "origin", cwd=checkout)
    git("switch", "-q", "-c", "topic", "origin/main", cwd=checkout)
    commit(checkout, "README.md", "changed")
    link(checkout, "github", "docs")
    assert _touched(env, m, rules, checkout, "topic")[1] == []


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
def test_touched_symlink_under_wildcard_prefix(env, m, rules, checkout, path, extra, hit):
    """A symlink matching any leading prefix of a pattern can lead into it; a leading ** is reached by all."""
    git("switch", "-q", "-c", "topic", cwd=checkout)
    commit(checkout, "real/skill.md")
    sha = link(checkout, path, "/".join([".."] * path.count("/") + ["real"]))
    hits = _touched(env, m, rules, checkout, "topic", DEFAULT_HANDOVER_PATHS + canon(*extra))[1]
    assert hits == ([sha] if hit else [])
