"""Typed GitHub reads: what agents used gh's read commands for."""

import base64

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


# --- issue ----------------------------------------------------------------------------------------

ISSUE = ("GET", "repos/me/app/issues/3")
ISSUES = "repos/me/app/issues?state={}&sort=updated&direction=desc&per_page=100&page={}"


def issue(number=3, comments=0, **extra):
    return {
        "number": number,
        "title": f"It breaks {number}",
        "state": "open",
        "state_reason": None,
        "user": {"login": "bob"},
        "labels": [{"name": "bug"}],
        "assignees": [{"login": "alice"}],
        "comments": comments,
        "created_at": "2026-10-20T09:00:00Z",
        "updated_at": "2026-10-26T10:00:00Z",
        "html_url": f"https://github.com/me/app/issues/{number}",
        "body": "Steps:\r\n1. \x1b[1mrun\x1b[0m it",
        **extra,
    }


def comment(i):
    return {"user": {"login": f"u{i}"}, "created_at": f"2026-10-26T10:{i % 60:02d}:00Z", "body": f"comment {i}"}


def test_issue_view(env, gh):
    gh.replies[ISSUE] = issue(comments=2)
    gh.replies[("GET", "repos/me/app/issues/3/comments?per_page=100&page=1")] = [comment(1), comment(2)]
    out = call(env, "issue", repo="me/app", action="view", number=3)
    assert requests(gh) == [ISSUE, ("GET", "repos/me/app/issues/3/comments?per_page=100&page=1")]
    assert out.startswith(UNTRUSTED + "\n")
    for part in (
        "Issue #3 of me/app: It breaks 3",
        "State: open",
        "Author: bob",
        "Labels: bug",
        "Assignees: alice",
        "https://github.com/me/app/issues/3",
        "Steps:\n1. run it",
        "Comments (2 of 2):",
        "u1 at 2026-10-26T10:01:00Z:\ncomment 1",
        "u2 at 2026-10-26T10:02:00Z:\ncomment 2",
    ):
        assert part in out, part


def test_issue_view_shows_the_last_twenty_comments(env, gh):
    gh.replies[ISSUE] = issue(comments=210)
    page2 = ("GET", "repos/me/app/issues/3/comments?per_page=100&page=2")
    page3 = ("GET", "repos/me/app/issues/3/comments?per_page=100&page=3")
    gh.replies[page3] = [comment(i) for i in range(200, 210)]
    gh.replies[page2] = [comment(i) for i in range(100, 200)]
    out = call(env, "issue", repo="me/app", action="view", number=3)
    assert requests(gh) == [ISSUE, page3, page2]
    assert "Comments (last 20 of 210):" in out
    assert "comment 190\n" in out and "comment 209" in out and "comment 189\n" not in out


def test_issue_view_closed(env, gh):
    gh.replies[ISSUE] = issue(state="closed", state_reason="not_planned")
    assert "State: closed (not_planned)" in call(env, "issue", repo="me/app", action="view", number=3)


def test_issue_view_missing(env, gh):
    gh.replies[ISSUE] = (404, "Not Found", {})
    with pytest.raises(Refused, match="has no issue #3"):
        call(env, "issue", repo="me/app", action="view", number=3)


def test_issue_view_without_the_permission(env, gh):
    gh.replies[ISSUE] = (403, "Resource not accessible by personal access token", {})
    with pytest.raises(Refused, match='"Issues"'):
        call(env, "issue", repo="me/app", action="view", number=3)


def test_issue_list_leaves_out_pull_requests(env, gh):
    path = ("GET", ISSUES.format("open", 1))
    gh.replies[path] = [issue(5), issue(4, pull_request={"url": "x"}), issue(3, labels=[])]
    out = call(env, "issue", repo="me/app", action="list")
    assert requests(gh) == [path]
    lines = out.splitlines()
    assert lines[1] == "Issues of me/app (open, most recently updated first):"
    assert lines[2:] == [
        "#5 It breaks 5 [open] labels: bug; updated 2026-10-26T10:00:00Z",
        "#3 It breaks 3 [open]; updated 2026-10-26T10:00:00Z",
    ]


def test_issue_list_filters_and_limit(env, gh):
    path = ("GET", ISSUES.format("all", 1) + "&labels=bug%2Cgood%20first%20issue")
    gh.replies[path] = [issue(i) for i in range(10, 0, -1)]
    out = call(env, "issue", repo="me/app", action="list", state="all", labels=["bug", "good first issue"], limit=3)
    assert requests(gh) == [path]
    assert len([line for line in out.splitlines() if line.startswith("#")]) == 3


@pytest.mark.parametrize(
    "kwargs",
    [{"state": "merged"}, {"limit": 101}, {"labels": "bug"}, {"labels": ["a,b"]}, {"labels": ["\x1b"]}, {"number": 3}],
)
def test_issue_list_refuses_bad_arguments(env, gh, kwargs):
    with pytest.raises(Refused):
        call(env, "issue", repo="me/app", action="list", **kwargs)
    assert requests(gh) == []


def test_issue_reads_need_only_read_access(env, gh):
    gh.replies[("GET", "repos/other/lib/issues/3")] = issue()
    call(env, "issue", repo="other/lib", action="view", number=3)
    with pytest.raises(Refused, match="read-only"):
        call(env, "issue", repo="other/lib", action="close", number=3)


@pytest.mark.parametrize(
    ("kwargs", "extra"),
    [
        ({"action": "view", "number": 3, "title": "T"}, "title"),
        ({"action": "close", "number": 3, "state": "open"}, "state"),
        ({"action": "create", "title": "T", "labels": ["bug"]}, "labels"),
    ],
)
def test_issue_actions_refuse_what_they_do_not_take(env, gh, kwargs, extra):
    with pytest.raises(Refused, match=f"does not take {extra}"):
        call(env, "issue", repo="me/app", **kwargs)
    assert requests(gh) == []


# --- runs -----------------------------------------------------------------------------------------

RUNS = "repos/me/app/actions/runs?per_page={}"


def wrun(i, **extra):
    return {
        "id": i,
        "name": "CI",
        "event": "push",
        "head_branch": "feat",
        "status": "completed",
        "conclusion": "success",
        "created_at": "2026-10-26T10:00:00Z",
        "html_url": f"https://github.com/me/app/actions/runs/{i}",
        **extra,
    }


def test_runs(env, gh):
    gh.replies[("GET", RUNS.format(10))] = {
        "total_count": 2,
        "workflow_runs": [wrun(7), wrun(6, status="in_progress", conclusion=None)],
    }
    out = call(env, "runs", repo="me/app")
    assert requests(gh) == [("GET", RUNS.format(10))]
    lines = out.splitlines()
    assert lines[0] == UNTRUSTED
    assert lines[1] == "Workflow runs of me/app, newest first:"
    assert (
        lines[2]
        == "- 7: CI, push on feat, completed, success, created 2026-10-26T10:00:00Z, https://github.com/me/app/actions/runs/7"
    )
    assert lines[3].startswith("- 6: CI, push on feat, in_progress, created")


def test_runs_filters(env, gh):
    path = "repos/me/app/actions/workflows/ci.yml/runs?per_page=50&branch=feat%2Fx&status=failure"
    gh.replies[("GET", path)] = {"workflow_runs": []}
    out = call(env, "runs", repo="me/app", branch="feat/x", workflow="ci.yml", status="failure", limit=50)
    assert requests(gh) == [("GET", path)]
    assert "No workflow runs" in out


def test_runs_of_a_workflow_by_id(env, gh):
    path = "repos/me/app/actions/workflows/123/runs?per_page=10"
    gh.replies[("GET", path)] = {"workflow_runs": [wrun(1)]}
    call(env, "runs", repo="me/app", workflow="123")
    assert requests(gh) == [("GET", path)]


def test_runs_of_a_missing_workflow(env, gh):
    gh.replies[("GET", "repos/me/app/actions/workflows/gone.yml/runs?per_page=10")] = (404, "Not Found", {})
    with pytest.raises(Refused, match="has no workflow gone.yml"):
        call(env, "runs", repo="me/app", workflow="gone.yml")


def test_runs_without_actions_read(env, gh):
    gh.replies[("GET", RUNS.format(10))] = (403, "Resource not accessible by personal access token", {})
    with pytest.raises(Refused, match='"Actions"'):
        call(env, "runs", repo="me/app")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"limit": 51},
        {"limit": 0},
        {"status": "broken"},
        {"workflow": "../x.yml"},
        {"workflow": "ci"},
        {"workflow": "a/b.yml"},
        {"branch": "a..b"},
    ],
)
def test_runs_refuses_bad_arguments(env, gh, kwargs):
    with pytest.raises(Refused):
        call(env, "runs", repo="me/app", **kwargs)
    assert requests(gh) == []


def test_runs_needs_read_access(env, gh):
    gh.replies[("GET", RUNS.format(10).replace("me/app", "other/lib"))] = {"workflow_runs": []}
    call(env, "runs", repo="other/lib")
    with pytest.raises(Refused, match="allowlist"):
        call(env, "runs", repo="nobody/x")


# --- release --------------------------------------------------------------------------------------


def rel(tag="v1.2.0", **extra):
    return {
        "tag_name": tag,
        "name": f"Release {tag}",
        "draft": False,
        "prerelease": False,
        "published_at": "2026-10-25T08:00:00Z",
        "author": {"login": "alice"},
        "html_url": f"https://github.com/me/app/releases/tag/{tag}",
        "assets": [{"name": "app.zip", "size": 1536}, {"name": "app.tar.gz", "size": 3 << 20}],
        "body": "## Changes\r\n- \x1b[32mfix\x1b[0m",
        **extra,
    }


def test_release_list(env, gh):
    path = ("GET", "repos/me/app/releases?per_page=10")
    gh.replies[path] = [rel(), rel("v1.3.0-rc1", prerelease=True), rel("v2", draft=True, published_at=None)]
    out = call(env, "release", repo="me/app", action="list")
    assert requests(gh) == [path]
    lines = out.splitlines()
    assert lines[1] == "Releases of me/app, newest first:"
    assert (
        lines[2]
        == "- v1.2.0: Release v1.2.0, published 2026-10-25T08:00:00Z, https://github.com/me/app/releases/tag/v1.2.0"
    )
    assert "[prerelease]" in lines[3] and "[draft]" in lines[4] and "not published" in lines[4]


def test_release_list_limit(env, gh):
    path = ("GET", "repos/me/app/releases?per_page=30")
    gh.replies[path] = []
    assert "has no releases" in call(env, "release", repo="me/app", action="list", limit=30)
    assert requests(gh) == [path]


@pytest.mark.parametrize(
    ("tag", "path"),
    [
        (None, "repos/me/app/releases/latest"),
        ("latest", "repos/me/app/releases/latest"),
        ("v1.2.0", "repos/me/app/releases/tags/v1%2E2%2E0"),
        ("release/v1", "repos/me/app/releases/tags/release/v1"),
    ],
)
def test_release_view(env, gh, tag, path):
    gh.replies[("GET", path)] = rel()
    out = call(env, "release", repo="me/app", action="view", **({"tag": tag} if tag else {}))
    assert requests(gh) == [("GET", path)]
    for part in (
        "Release v1.2.0 of me/app (tag v1.2.0)",
        "Published: 2026-10-25T08:00:00Z by alice",
        "Assets: app.zip (1.5 KiB), app.tar.gz (3.0 MiB)",
        "## Changes\n- fix",
    ):
        assert part in out, part


def test_release_view_missing(env, gh):
    gh.replies[("GET", "repos/me/app/releases/tags/v9")] = (404, "Not Found", {})
    with pytest.raises(Refused, match="has no release with tag v9"):
        call(env, "release", repo="me/app", action="view", tag="v9")
    gh.replies[("GET", "repos/me/app/releases/latest")] = (404, "Not Found", {})
    with pytest.raises(Refused, match="has no published release"):
        call(env, "release", repo="me/app", action="view")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"action": "download", "tag": "v1"},
        {"action": "create", "tag": "v1"},
        {"action": "list", "limit": 31},
        {"action": "list", "tag": "v1"},
        {"action": "view", "limit": 3},
        {"action": "view", "tag": "a/../b"},
        {"action": "view", "tag": ""},
    ],
)
def test_release_refuses(env, gh, kwargs):
    with pytest.raises(Refused):
        call(env, "release", repo="me/app", **kwargs)
    assert requests(gh) == []


def test_release_download_says_it_is_not_supported(env, gh):
    with pytest.raises(Refused, match="not supported"):
        call(env, "release", repo="me/app", action="download", tag="v1")


# --- file -----------------------------------------------------------------------------------------


def blob(content: bytes, path="docs/READ ME.md", **extra):
    encoded = base64.encodebytes(content).decode()  # GitHub wraps its base64 at 60 characters
    return {"type": "file", "path": path, "size": len(content), "sha": "b" * 40, "encoding": "base64",
            "content": encoded, **extra}  # fmt: skip


def test_file(env, gh):
    path = ("GET", "repos/me/app/contents/docs/READ%20ME.md")
    content = b"# Title\r\n\x1b[1mbold\x1b[0m\tand tab\n" * 3
    gh.replies[path] = blob(content)
    out = call(env, "file", repo="me/app", path="docs/READ ME.md")
    assert requests(gh) == [path]
    lines = out.splitlines()
    assert lines[0] == UNTRUSTED
    assert lines[1] == f"docs/READ ME.md at the default branch ({len(content)} bytes):"
    assert lines[2:4] == ["# Title", "bold\tand tab"]


def test_file_at_a_ref(env, gh):
    path = ("GET", "repos/me/app/contents/a/b.txt?ref=feat%2Fx")
    gh.replies[path] = blob(b"hi\n", path="a/b.txt")
    out = call(env, "file", repo="me/app", path="a/b.txt", ref="feat/x")
    assert requests(gh) == [path]
    assert "a/b.txt at feat/x (3 bytes):\nhi" in out


def test_a_directory_is_listed(env, gh):
    path = ("GET", "repos/me/app/contents/src")
    gh.replies[path] = [
        {"type": "dir", "name": "git_gate", "path": "src/git_gate"},
        {"type": "file", "name": "x.py", "path": "src/x.py", "size": 12},
        {"type": "symlink", "name": "l", "path": "src/l", "size": 3},
    ]
    out = call(env, "file", repo="me/app", path="src")
    assert out.splitlines()[1:] == [
        "src at the default branch is a directory:",
        "git_gate/",
        "x.py (12 B)",
        "l (symlink)",
    ]


def test_the_root_is_listed(env, gh):
    gh.replies[("GET", "repos/me/app/contents/")] = [{"type": "file", "name": "README.md", "size": 5}]
    assert "README.md (5 B)" in call(env, "file", repo="me/app", path="")


def test_a_binary_file_is_refused_with_its_size(env, gh):
    gh.replies[("GET", "repos/me/app/contents/logo.png")] = blob(b"\x89PNG\r\n\x1a\n\0\0\0", path="logo.png")
    with pytest.raises(Refused, match=r"logo.png is binary \(11 B\)"):
        call(env, "file", repo="me/app", path="logo.png")


def test_a_file_over_a_mebibyte_is_refused(env, gh):
    gh.replies[("GET", "repos/me/app/contents/big.txt")] = blob(b"", path="big.txt", size=(1 << 20) + 1)
    with pytest.raises(Refused, match="big.txt is 1.0 MiB"):
        call(env, "file", repo="me/app", path="big.txt")


def test_a_long_text_file_is_cut(env, gh):
    gh.replies[("GET", "repos/me/app/contents/long.txt")] = blob(b"x" * (MAX_OUTPUT + 10_000) + b"\n", path="long.txt")
    out = call(env, "file", repo="me/app", path="long.txt")
    assert len(out) < MAX_OUTPUT + 500 and "clone or fetch" in out


def test_a_missing_file(env, gh):
    gh.replies[("GET", "repos/me/app/contents/nope.md?ref=v1")] = (404, "Not Found", {})
    with pytest.raises(Refused, match="has no nope.md at v1") as e:
        call(env, "file", repo="me/app", path="nope.md", ref="v1")
    assert UNTRUSTED not in str(e.value)


def test_file_without_contents_read(env, gh):
    gh.replies[("GET", "repos/me/app/contents/x")] = (403, "Resource not accessible by personal access token", {})
    with pytest.raises(Refused, match='"Contents"'):
        call(env, "file", repo="me/app", path="x")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"path": "../other/x"},
        {"path": "a/../../b"},
        {"path": "./a"},
        {"path": "/etc/passwd"},
        {"path": "a//b"},
        {"path": "a?ref=x"},
        {"path": "a#b"},
        {"path": "a\nb"},
        {"path": None},
        {"path": "a", "ref": "-x"},
        {"path": "a", "ref": "a b"},
        {"path": "a", "ref": ""},
    ],
)
def test_file_refuses_bad_paths_and_refs(env, gh, kwargs):
    with pytest.raises(Refused):
        call(env, "file", repo="me/app", **kwargs)
    assert requests(gh) == []


def test_file_needs_read_access(env, gh):
    with pytest.raises(Refused, match="allowlist"):
        call(env, "file", repo="nobody/x", path="a")


# --- api_get --------------------------------------------------------------------------------------


def test_api_get(env, gh):
    path = ("GET", "repos/me/app/releases/latest")
    gh.replies[path] = {"tag_name": "v1", "body": "a\x1b[2Jb ‮"}
    out = call(env, "api_get", repo="me/app", path="releases/latest")
    assert requests(gh) == [path]
    assert out.startswith(UNTRUSTED + "\n{\n")
    assert '"tag_name": "v1"' in out and "\x1b" not in out and "‮" not in out


def test_api_get_of_the_repo_itself(env, gh):
    gh.replies[("GET", "repos/me/app")] = {"full_name": "me/app", "default_branch": "main"}
    assert '"default_branch": "main"' in call(env, "api_get", repo="me/app", path="")


def test_api_get_with_a_query(env, gh):
    path = ("GET", "repos/me/app/actions/workflows?per_page=5&page=2&flag=true&q=a+b%26c")
    gh.replies[path] = {"workflows": []}
    call(
        env,
        "api_get",
        repo="me/app",
        path="actions/workflows",
        query={"per_page": 5, "page": "2", "flag": True, "q": "a b&c"},
    )
    assert requests(gh) == [path]


def test_api_get_keeps_dots_inside_segments(env, gh):
    path = ("GET", "repos/me/app/contents/docs/x.md")
    gh.replies[path] = {"name": "x.md"}
    call(env, "api_get", repo="me/app", path="contents/docs/x.md")
    assert requests(gh) == [path]


def test_api_get_cuts_a_long_answer(env, gh):
    gh.replies[("GET", "repos/me/app/labels")] = [{"name": f"label {i}", "description": "d" * 100} for i in range(5000)]
    out = call(env, "api_get", repo="me/app", path="labels")
    assert len(out) < MAX_OUTPUT + 500 and "characters cut" in out


def test_api_get_refuses_a_huge_answer(env, gh):
    from git_gate.github import MAX_API

    gh.replies[("GET", "repos/me/app/labels")] = b"[" + b'"x",' * (MAX_API // 4) + b'"x"]'
    with pytest.raises(Refused, match="query"):
        call(env, "api_get", repo="me/app", path="labels")


def test_api_get_does_not_follow_a_download(env, gh):
    from git_gate.github import Redirected

    gh.replies[("GET", "repos/me/app/zipball/main")] = Redirected("redirected", "https://codeload.example/sig=SECRET")
    with pytest.raises(Refused, match="download") as e:
        call(env, "api_get", repo="me/app", path="zipball/main")
    assert "SECRET" not in str(e.value)


def test_api_get_explains_a_404(env, gh):
    gh.replies[("GET", "repos/me/app/environments")] = (404, "Not Found", {})
    with pytest.raises(Refused, match="Not Found.*permission") as e:
        call(env, "api_get", repo="me/app", path="environments")
    assert str(e.value).startswith(UNTRUSTED)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"path": "../other"},
        {"path": "issues/../../../other/x/issues"},
        {"path": "/repos/me/app"},
        {"path": "https://api.github.com/repos/other/x"},
        {"path": "//evil.example/x"},
        {"path": "graphql"},
        {"path": "repos/me/app/issues"},
        {"path": "issues?state=all"},
        {"path": "issues#x"},
        {"path": "issues\\x"},
        {"path": "issues", "query": "state=all"},
        {"path": "issues", "query": {"state": ["all"]}},
        {"path": "issues", "query": {"a\n": "b"}},
        {"path": "issues", "query": {"a": "b\x1b"}},
    ],
)
def test_api_get_refuses(env, gh, kwargs):
    with pytest.raises(Refused):
        call(env, "api_get", repo="me/app", **kwargs)
    assert requests(gh) == []


def test_api_get_needs_read_access(env, gh):
    with pytest.raises(Refused, match="allowlist"):
        call(env, "api_get", repo="nobody/x", path="")


# --- search ---------------------------------------------------------------------------------------


def searched(q: str, per_page: int = 30) -> tuple[str, str]:
    from urllib.parse import quote

    return ("GET", f"search/issues?q={quote(q, safe='')}&sort=updated&order=desc&per_page={per_page}")


def hit(number, *, pull=False, merged=False, **extra):
    item = issue(number, **extra)
    if pull:
        item["pull_request"] = {"url": "x", "merged_at": "2026-10-26T11:00:00Z" if merged else None}
        item["draft"] = extra.get("draft", False)
    return item


def test_pr_search(env, gh):
    path = searched("repo:me/app is:pr is:open flaky test")
    gh.replies[path] = {"total_count": 2, "items": [hit(12, pull=True), hit(9, pull=True, draft=True)]}
    out = call(env, "pr", repo="me/app", action="list", search="flaky test")
    assert requests(gh) == [path]
    lines = out.splitlines()
    assert lines[0] == UNTRUSTED
    assert lines[1] == 'Pull requests of me/app matching "flaky test" (open, most recently updated first; 2 found):'
    assert lines[2] == "#12 It breaks 12 [open] labels: bug; updated 2026-10-26T10:00:00Z"
    assert lines[3].startswith("#9 It breaks 9 [open, draft]")


def test_pr_search_with_every_filter(env, gh):
    path = searched("repo:me/app is:pr is:merged head:feat base:main a b", 5)
    gh.replies[path] = {"items": [hit(4, pull=True, merged=True, state="closed")]}
    out = call(env, "pr", repo="me/app", action="list", search="a b", state="merged", head="feat", base="main", limit=5)
    assert requests(gh) == [path]
    assert "#4 It breaks 4 [merged]" in out


def test_issue_search(env, gh):
    path = searched('repo:me/app is:issue label:"bug" label:"good first issue" crash -wontfix', 10)
    gh.replies[path] = {"total_count": 1, "items": [hit(3)]}
    out = call(env, "issue", repo="me/app", action="list", search="crash -wontfix", state="all",
               labels=["bug", "good first issue"], limit=10)  # fmt: skip
    assert requests(gh) == [path]
    assert out.splitlines()[2] == "#3 It breaks 3 [open] labels: bug; updated 2026-10-26T10:00:00Z"


def test_search_with_no_hits(env, gh):
    gh.replies[searched("repo:me/app is:issue is:closed nothing")] = {"total_count": 0, "items": []}
    out = call(env, "issue", repo="me/app", action="list", search="nothing", state="closed")
    assert 'No issues of me/app match "nothing" (state closed).' in out


def test_search_rate_limit_says_try_again_in_a_minute(env, gh):
    gh.replies[searched("repo:me/app is:pr is:open x")] = (403, "API rate limit exceeded for search", {})
    with pytest.raises(Refused, match="try again in a minute"):
        call(env, "pr", repo="me/app", action="list", search="x")
    assert len(requests(gh)) == 1


def test_search_needs_read_access(env, gh):
    gh.replies[searched("repo:other/lib is:issue is:open x")] = {"items": []}
    call(env, "issue", repo="other/lib", action="list", search="x")
    with pytest.raises(Refused, match="allowlist"):
        call(env, "issue", repo="nobody/x", action="list", search="x")


@pytest.mark.parametrize(
    "text",
    [
        "repo:other/x",
        "REPO:other/x",
        "fix -repo:me/app",
        'fix "repo:other/x"',
        "fix (repo:other/x)",
        "org:github",
        "user:octocat bug",
        "Owner:octocat",
        "is:public",
        "is:PRIVATE",
        'is:"public"',
        "fix,repo:other/x",
        "repo :other/x",
        "a OR author:x",
        "a AND b",
        "NOT a",
        "a) OR (b",
        "tab\there",
        "line\nbreak",
        "\x1b[2J",
        "x" * 257,
        "",
        "   ",
    ],
)
@pytest.mark.parametrize("tool", ["pr", "issue"])
def test_search_refuses_what_could_widen_or_break_it(env, gh, tool, text):
    with pytest.raises(Refused) as e:
        call(env, tool, repo="me/app", action="list", search=text)
    assert requests(gh) == []
    assert UNTRUSTED not in str(e.value)


@pytest.mark.parametrize("text", ["or", "repository layout", "isn't public", "user-facing bug", "org chart", "x" * 256])
def test_search_lets_plain_words_through(env, gh, text):
    gh.replies[searched(f"repo:me/app is:issue is:open {text}")] = {"items": []}
    call(env, "issue", repo="me/app", action="list", search=text)
    assert len(requests(gh)) == 1


@pytest.mark.parametrize(
    "kwargs",
    [{"head": "someone:fix"}, {"labels": ['a"b']}, {"search": 5}],
)
def test_search_refuses_filters_it_cannot_express(env, gh, kwargs):
    tool = "issue" if "labels" in kwargs else "pr"
    with pytest.raises(Refused):
        call(env, tool, repo="me/app", action="list", **({"search": "x"} | kwargs))
    assert requests(gh) == []


def test_search_is_only_for_list(env, gh):
    with pytest.raises(Refused, match="does not take search"):
        call(env, "pr", repo="me/app", action="view", number=12, search="x")
    with pytest.raises(Refused, match="does not take search"):
        call(env, "issue", repo="me/app", action="create", title="T", search="x")


def test_the_client_scopes_every_search_itself():
    from conftest import FakeOpener
    from git_gate.github import GitHub

    client = GitHub("tok")
    client._opener = FakeOpener({searched("repo:me/app is:pr x", 3): {"items": []}})
    client.search_issues("me/app", "pr", "x", 3)
    assert [p for _, p, _ in client._opener.calls] == [searched("repo:me/app is:pr x", 3)[1]]
    with pytest.raises(Refused):
        client.search_issues("me/app", "discussion", "x", 3)


@pytest.mark.parametrize(
    "label",
    [
        'bug\\" repo:x/y',
        'bug" repo:x/y "',
        "a OR b",
        "a AND b",
        "NOT a",
        "fix repo:x/y",
        "x org:github",
        "is:public",
        "status:done",
        "a\\b",
        "a(b)",
    ],
)
def test_a_label_cannot_smuggle_a_qualifier_into_a_search(env, gh, label):
    with pytest.raises(Refused) as e:
        call(env, "issue", repo="me/app", action="list", search="crash", labels=[label])
    assert requests(gh) == []
    assert UNTRUSTED not in str(e.value)


@pytest.mark.parametrize("text", ["a\\b", 'say "hi"', "fix\\", "a(b"])
def test_search_text_refuses_backslashes_quotes_and_parentheses(env, gh, text):
    with pytest.raises(Refused):
        call(env, "issue", repo="me/app", action="list", search=text)
    assert requests(gh) == []


@pytest.mark.parametrize("label", ["good first issue", "help-wanted", "v1.2", "area/ci", "won't fix", "P1!"])
def test_plain_labels_still_search(env, gh, label):
    gh.replies[searched(f'repo:me/app is:issue is:open label:"{label}" crash')] = {"items": [hit(3)]}
    out = call(env, "issue", repo="me/app", action="list", search="crash", labels=[label])
    assert len(requests(gh)) == 1 and "#3 It breaks 3" in out
