"""Typed GitHub reads: what agents used gh's read commands for."""

import pytest

from conftest import TOKEN, FakeOpener, call
from git_gate import Refused
from git_gate.gate import MAX_OUTPUT
from git_gate.github import GitHub
from git_gate.writes import UNTRUSTED

PULL = ("GET", "repos/me/app/pulls/12")
REVIEWS = ("GET", "repos/me/app/pulls/12/reviews?per_page=100")
PULLS = "repos/me/app/pulls?state={}&sort=updated&direction=desc&per_page={}"


def pull(number=12, state="open", **extra):
    return {
        "number": number,
        "title": f"Add the thing {number}",
        "state": state,
        "draft": False,
        "merged_at": None,
        "user": {"login": "alice"},
        "head": {"ref": "feat", "sha": "a" * 40},
        "base": {"ref": "main"},
        "mergeable": True,
        "mergeable_state": "clean",
        "labels": [{"name": "bug"}, {"name": "good first issue"}],
        "requested_reviewers": [{"login": "carol"}],
        "requested_teams": [{"slug": "core"}],
        "html_url": f"https://github.com/me/app/pull/{number}",
        "updated_at": "2026-10-26T10:00:00Z",
        "body": "Fixes #3.\r\n\r\n\x1b[31mred\x1b[0m line",
        **extra,
    }


@pytest.fixture
def gh(env):
    """A real client over canned GitHub answers; each test fills `gh.replies`."""
    client = GitHub(TOKEN)
    client._opener = FakeOpener({PULL: pull(), REVIEWS: []})
    client.replies = client._opener.replies

    def factory(token):
        assert token == TOKEN
        return client

    env.gate.github = factory
    return client


def requests(gh) -> list[tuple[str, str]]:
    return [(method, path) for method, path, _ in gh._opener.calls]


# --- pr view --------------------------------------------------------------------------------------


def test_pr_view(env, gh):
    gh.replies[REVIEWS] = [
        {"user": {"login": "bob"}, "state": "CHANGES_REQUESTED"},
        {"user": {"login": "dave"}, "state": "COMMENTED"},
        {"user": {"login": "bob"}, "state": "APPROVED"},
    ]
    out = call(env, "pr", repo="me/app", action="view", number=12)
    assert requests(gh) == [PULL, REVIEWS]
    assert out.startswith(UNTRUSTED + "\n")
    for part in (
        "Pull request #12 of me/app: Add the thing 12",
        "State: open",
        "Author: alice",
        "Branches: feat -> main",
        "Mergeable: clean",
        "Labels: bug, good first issue",
        "Requested reviewers: carol, core (team)",
        "Reviews: bob approved, dave commented",
        "https://github.com/me/app/pull/12",
        "Fixes #3.\n\nred line",
    ):
        assert part in out, part
    assert "\x1b" not in out and "\r" not in out


@pytest.mark.parametrize(
    ("extra", "state"),
    [
        ({"draft": True}, "State: open, draft"),
        ({"state": "closed", "merged_at": "2026-10-26T11:00:00Z"}, "State: merged"),
    ],
)
def test_pr_view_states(env, gh, extra, state):
    gh.replies[PULL] = pull(**extra)
    assert state in call(env, "pr", repo="me/app", action="view", number=12)


def test_pr_view_of_a_missing_pull_request(env, gh):
    gh.replies[PULL] = (404, "Not Found", {})
    with pytest.raises(Refused, match="has no pull request #12") as e:
        call(env, "pr", repo="me/app", action="view", number=12)
    assert UNTRUSTED not in str(e.value)


def test_pr_view_without_the_permission(env, gh):
    gh.replies[PULL] = (403, "Resource not accessible by personal access token", {})
    with pytest.raises(Refused, match='"Pull requests"') as e:
        call(env, "pr", repo="me/app", action="view", number=12)
    assert str(e.value).startswith(UNTRUSTED)


# --- pr list --------------------------------------------------------------------------------------


def test_pr_list(env, gh):
    path = PULLS.format("open", 30)
    gh.replies[("GET", path)] = [pull(12), pull(9, draft=True)]
    out = call(env, "pr", repo="me/app", action="list")
    assert requests(gh) == [("GET", path)]
    lines = out.splitlines()
    assert lines[1] == "Pull requests of me/app (open, most recently updated first):"
    assert lines[2] == "#12 Add the thing 12 [open] feat -> main, updated 2026-10-26T10:00:00Z"
    assert lines[3] == "#9 Add the thing 9 [open, draft] feat -> main, updated 2026-10-26T10:00:00Z"


def test_pr_list_filters(env, gh):
    path = PULLS.format("all", 5) + "&head=me%3Afeat&base=main"
    gh.replies[("GET", path)] = []
    out = call(env, "pr", repo="me/app", action="list", state="all", head="feat", base="main", limit=5)
    assert requests(gh) == [("GET", path)]
    assert "No pull requests" in out


def test_pr_list_head_of_a_fork(env, gh):
    path = PULLS.format("open", 30) + "&head=someone%3Afix"
    gh.replies[("GET", path)] = []
    call(env, "pr", repo="me/app", action="list", head="someone:fix")
    assert requests(gh) == [("GET", path)]


def test_pr_list_merged_pages_closed_ones(env, gh):
    page1 = ("GET", PULLS.format("closed", 100) + "&page=1")
    page2 = ("GET", PULLS.format("closed", 100) + "&page=2")
    merged = {"merged_at": "2026-10-26T11:00:00Z"}
    gh.replies[page1] = [pull(i, "closed", **(merged if i % 2 else {})) for i in range(100, 0, -1)]
    gh.replies[page2] = [pull(i, "closed", **merged) for i in range(500, 450, -1)]
    out = call(env, "pr", repo="me/app", action="list", state="merged", limit=60)
    assert requests(gh) == [page1, page2]
    entries = [line for line in out.splitlines() if line.startswith("#")]
    assert len(entries) == 60 and all("[merged]" in line for line in entries)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"state": "draft"},
        {"limit": 0},
        {"limit": 101},
        {"limit": True},
        {"head": "../x"},
        {"base": "a..b"},
        {"head": "a:b:c"},
        {"number": 3},
        {"title": "x"},
    ],
)
def test_pr_list_refuses_bad_arguments(env, gh, kwargs):
    with pytest.raises(Refused):
        call(env, "pr", repo="me/app", action="list", **kwargs)
    assert requests(gh) == []


# --- pr diff --------------------------------------------------------------------------------------


def test_pr_diff(env, gh):
    gh.replies[PULL] = b"diff --git a/x b/x\n+\x1b[2Jadded\n"
    out = call(env, "pr", repo="me/app", action="diff", number=12)
    assert requests(gh) == [PULL]
    assert gh._opener.headers[0]["accept"] == "application/vnd.github.diff"
    assert out.startswith(UNTRUSTED + "\ndiff --git a/x b/x\n+added")


def test_a_long_diff_is_cut(env, gh):
    gh.replies[PULL] = b"+" + b"x" * (MAX_OUTPUT * 3) + b"\n"
    out = call(env, "pr", repo="me/app", action="diff", number=12)
    assert len(out) < MAX_OUTPUT + 500
    assert out.rstrip().endswith("characters cut: the diff is longer than the gate shows.]")


def test_a_missing_pull_request_has_no_diff(env, gh):
    gh.replies[PULL] = (404, "Not Found", {})
    with pytest.raises(Refused, match="has no pull request #12"):
        call(env, "pr", repo="me/app", action="diff", number=12)


# --- read and write access ------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["view", "diff"])
def test_pr_reads_need_only_read_access(env, gh, action):
    gh.replies[("GET", "repos/other/lib/pulls/12")] = (b"x") if action == "diff" else pull()
    call(env, "pr", repo="other/lib", action=action, number=12)
    with pytest.raises(Refused, match="allowlist"):
        call(env, "pr", repo="nobody/x", action=action, number=12)


def test_pr_writes_still_need_write_access(env, gh):
    with pytest.raises(Refused, match="read-only"):
        call(env, "pr", repo="other/lib", action="close", number=12)


@pytest.mark.parametrize(
    ("kwargs", "extra"),
    [
        ({"action": "view", "number": 12, "title": "T"}, "title"),
        ({"action": "diff", "number": 12, "limit": 3}, "limit"),
        ({"action": "view", "number": 12, "draft": True}, "draft"),
        ({"action": "close", "number": 12, "state": "open"}, "state"),
        ({"action": "create", "title": "T", "head": "feat", "limit": 3}, "limit"),
    ],
)
def test_pr_actions_refuse_what_they_do_not_take(env, gh, kwargs, extra):
    with pytest.raises(Refused, match=f"does not take {extra}"):
        call(env, "pr", repo="me/app", **kwargs)
    assert requests(gh) == []


def test_pr_names_its_read_actions(env, gh):
    with pytest.raises(Refused, match="view, list, diff"):
        call(env, "pr", repo="me/app", action="show", number=12)
