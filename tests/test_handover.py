import io
import json
import stat
from concurrent.futures import ThreadPoolExecutor

import pytest

from conftest import TOKEN, commit, git
from git_gate import Refused
from git_gate.config import Rules
from git_gate.handover import EXPIRES, Handover, Handovers, main, push_command
from git_gate.mirror import Mirror

REPO = "me/app"
DAY = 24 * 3600


class Clock:
    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


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


def make(hs: Handovers, **kw) -> Handover:
    args = {"repo": REPO, "branch": "feat", "sha": sha(), "base": None, "lease": None, "reason": "protected"}
    return hs.create(**(args | kw))


# --- records ------------------------------------------------------------------------------


def test_create_get_open_close(env, hs):
    h = make(hs, base=sha(2), lease=sha(3), reason="needs a force")
    assert (h.id, h.repo, h.branch, h.sha, h.base, h.lease) == (1, REPO, "feat", sha(), sha(2), sha(3))
    assert h.reason == "needs a force"
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
    assert hs.open() == [h]
    for bad in (9, 10):
        with pytest.raises(Refused, match="unreadable"):
            hs.get(bad)


# --- the command --------------------------------------------------------------------------


def test_push_command_shapes(tmp_path):
    mirror = tmp_path / "me/app.git"
    url = "git@github.com:me/app.git"
    plain = Handover(1, REPO, "feat", sha(), None, None, "protected", 0.0)
    assert push_command(plain, mirror, url) == [
        "git", "-c", "core.hooksPath=/dev/null", f"--git-dir={mirror}", "push", url, f"{sha()}:refs/heads/feat",
    ]  # fmt: skip
    rewrite = Handover(2, REPO, "feat", sha(), sha(2), sha(3), "rewrite", 0.0)
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
    git(
        "bundle",
        "create",
        "-q",
        str(env.state / "staging" / f"{branch}.bundle"),
        branch,
        "--not",
        "--remotes=origin",
        cwd=checkout,
    )
    with m.lock(REPO):
        m.sync(REPO, TOKEN, rules)
        tip = m.import_bundle(REPO, env.state / "staging" / f"{branch}.bundle", branch, rules)
        h = Handovers(env.state).create(REPO, branch, tip, base, lease, reason)
        m.keep(REPO, f"refs/gate/handover/{h.id}", tip, rules)
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
        m.sync(REPO, TOKEN, rules)
        tip = m.import_bundle(REPO, env.state / "staging" / "evil.bundle", "evil", rules)
        h = Handovers(env.state).create(REPO, "evil", tip, None, None, "protected")
        m.keep(REPO, f"refs/gate/handover/{h.id}", tip, rules)
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
        old = Handovers(env.state, clock=clock, mirror=m).create(REPO, "feat", tip, None, None, "protected")
        m.keep(REPO, old.ref, tip, rules)
    assert mirror_refs(m) == old.ref

    hs = Handovers(env.state, clock=Clock(clock.now + 10 * DAY), mirror=m)
    young = hs.create(REPO, "feat", tip, None, None, "protected")
    with m.lock(REPO):
        m.keep(REPO, young.ref, tip, rules)
    assert sorted(mirror_refs(m).split()) == [old.ref, young.ref]  # not expired yet

    hs = Handovers(env.state, clock=Clock(clock.now + 15 * DAY), mirror=m)
    hs.create(REPO, "other", tip, None, None, "protected")
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
