import io
import json
import urllib.error
import urllib.request

import pytest

from conftest import URL, FakeOpener, FakeResponse
from git_gate import Refused
from git_gate.github import MAX_LOG, FromGitHub, GitHub, GitHubError, RateLimited, Redirected, _SameHostRedirects


def make(replies=None):
    gh = GitHub("tok")
    gh._opener = FakeOpener(replies)
    return gh


RULES = ("GET", "repos/me/app/rules/branches/main")


@pytest.mark.parametrize(
    "message",
    [
        "Upgrade to GitHub Pro or make this repository public to enable this feature.",
        "Upgrade to GitHub Pro to enable this feature.",
        "Make this repository public to enable it.",
    ],
)
def test_rules_plan_limit_403_means_no_rules(message):
    gh = make({RULES: (403, message, {})})
    assert gh.rule_types("me/app", "main") == set()


@pytest.mark.parametrize(
    ("message", "headers"),
    [
        ("API rate limit exceeded for user.", {"x-ratelimit-remaining": "0"}),
        ("Resource protected by organization SAML enforcement.", {"x-github-sso": "required; url=x"}),
        ("Resource not accessible by personal access token", {}),
    ],
)
def test_rules_other_403_refuses(message, headers):
    gh = make({RULES: (403, message, headers)})
    with pytest.raises(Refused, match="403"):
        gh.rule_types("me/app", "main")


def test_rules_403_without_a_readable_body_refuses():
    gh = make({RULES: 403})
    with pytest.raises(Refused, match="403"):
        gh.rule_types("me/app", "main")


def test_rules_404_means_no_rules():
    gh = make({("GET", "repos/me/app/rules/branches/main"): 404})
    assert gh.rule_types("me/app", "main") == set()


def test_other_errors_still_refuse():
    gh = make({("GET", "repos/me/app/rules/branches/main"): 500})
    with pytest.raises(Refused, match="500"):
        gh.rule_types("me/app", "main")


def test_missing_branch_is_none():
    gh = make({("GET", "repos/me/app/branches/gone"): 404})
    assert gh.branch("me/app", "gone") is None


def test_json_body_and_headers_are_sent():
    gh = make()
    gh.comment("me/app", 7, "hi")
    assert gh._opener.calls == [("POST", "repos/me/app/issues/7/comments", {"body": "hi"})]


CASES = [
    (lambda g: g.comment("me/app", 7, "hi"), [("POST", "repos/me/app/issues/7/comments", {"body": "hi"})]),
    (
        lambda g: g.edit("me/app", 7, pull=True, base="dev"),
        [("PATCH", "repos/me/app/pulls/7", {"base": "dev"})],
    ),
    (
        lambda g: g.edit("me/app", 7, pull=False, state="closed"),
        [("PATCH", "repos/me/app/issues/7", {"state": "closed"})],
    ),
    (
        lambda g: g.edit("me/app", 7, pull=True, title="t", body="b"),
        [("PATCH", "repos/me/app/pulls/7", {"title": "t", "body": "b"})],
    ),
    (
        lambda g: g.create_issue("me/app", "t", "b"),
        [("POST", "repos/me/app/issues", {"title": "t", "body": "b"})],
    ),
    (
        lambda g: g.create_pull("me/app", "feat", "main", "t", "b", True),
        [
            (
                "POST",
                "repos/me/app/pulls",
                {"head": "feat", "base": "main", "title": "t", "body": "b", "draft": True},
            )
        ],
    ),
    (
        lambda g: g.review("me/app", 7, "COMMENT", "ok"),
        [("POST", "repos/me/app/pulls/7/reviews", {"event": "COMMENT", "body": "ok"})],
    ),
    (
        lambda g: g.review("me/app", 7, "REQUEST_CHANGES", "no"),
        [("POST", "repos/me/app/pulls/7/reviews", {"event": "REQUEST_CHANGES", "body": "no"})],
    ),
    (
        lambda g: g.labels("me/app", 7, ["a"], ["b c"]),
        [
            ("POST", "repos/me/app/issues/7/labels", {"labels": ["a"]}),
            ("DELETE", "repos/me/app/issues/7/labels/b%20c", None),
        ],
    ),
    (
        lambda g: g.assignees("me/app", 7, [], ["u"]),
        [("DELETE", "repos/me/app/issues/7/assignees", {"assignees": ["u"]})],
    ),
    (
        lambda g: g.assignees("me/app", 7, ["v"], []),
        [("POST", "repos/me/app/issues/7/assignees", {"assignees": ["v"]})],
    ),
    (
        lambda g: g.request_reviewers("me/app", 7, ["r"], []),
        [("POST", "repos/me/app/pulls/7/requested_reviewers", {"reviewers": ["r"]})],
    ),
    (
        lambda g: g.request_reviewers("me/app", 7, [], ["r"]),
        [("DELETE", "repos/me/app/pulls/7/requested_reviewers", {"reviewers": ["r"]})],
    ),
    (
        lambda g: g.request_reviewers("me/app", 7, ["a"], ["b"]),
        [
            ("POST", "repos/me/app/pulls/7/requested_reviewers", {"reviewers": ["a"]}),
            ("DELETE", "repos/me/app/pulls/7/requested_reviewers", {"reviewers": ["b"]}),
        ],
    ),
    (lambda g: g.request_reviewers("me/app", 7, [], []), []),
    (lambda g: g.labels("me/app", 7, [], []), []),
    (lambda g: g.assignees("me/app", 7, [], []), []),
    (
        lambda g: g.labels("me/app", 7, [], ["v1.0"]),
        [("DELETE", "repos/me/app/issues/7/labels/v1%2E0", None)],
    ),
    (
        lambda g: g.run_action("me/app", 5, "rerun"),
        [("POST", "repos/me/app/actions/runs/5/rerun", {})],
    ),
    (
        lambda g: g.run_action("me/app", 5, "rerun_failed"),
        [("POST", "repos/me/app/actions/runs/5/rerun-failed-jobs", {})],
    ),
    (
        lambda g: g.run_action("me/app", 5, "cancel"),
        [("POST", "repos/me/app/actions/runs/5/cancel", {})],
    ),
    (
        lambda g: g.label("me/app", "create", "bug", color="f00", description="d"),
        [("POST", "repos/me/app/labels", {"name": "bug", "color": "f00", "description": "d"})],
    ),
    (
        lambda g: g.label("me/app", "edit", "bug", new_name="defect"),
        [("PATCH", "repos/me/app/labels/bug", {"new_name": "defect"})],
    ),
]


@pytest.mark.parametrize(("act", "expected"), CASES)
def test_write_endpoints(act, expected):
    gh = make()
    act(gh)
    assert gh._opener.calls == expected


def test_writes_return_html_url():
    gh = make()
    assert gh.comment("me/app", 7, "x") == URL
    assert gh.edit("me/app", 7, pull=True, title="t") == URL
    assert gh.create_issue("me/app", "t", "b") == URL
    assert gh.create_pull("me/app", "f", "main", "t", "b", False) == URL
    assert gh.review("me/app", 7, "COMMENT", "x") == URL


@pytest.mark.parametrize("event", ["APPROVE", "approve", "", "DISMISS"])
def test_review_never_approves(event):
    gh = make()
    with pytest.raises(Refused, match="^Approving is the user's"):
        gh.review("me/app", 7, event, "x")
    assert gh._opener.calls == []


def test_run_action_rejects_unknown():
    gh = make()
    with pytest.raises(Refused):
        gh.run_action("me/app", 5, "delete")
    assert gh._opener.calls == []


@pytest.mark.parametrize("action", ["delete", "rename", ""])
def test_label_rejects_other_actions(action):
    gh = make()
    with pytest.raises(Refused):
        gh.label("me/app", action, "bug")
    assert gh._opener.calls == []


@pytest.mark.parametrize("name", ["..", ".", "", "..."])
def test_label_refuses_dot_segments(name):
    gh = make()
    with pytest.raises(Refused, match="segment"):
        gh.label("me/app", "edit", name, new_name="x")
    with pytest.raises(Refused):
        gh.labels("me/app", 7, [], [name])
    assert gh._opener.calls == []


def test_removing_an_absent_label_is_fine():
    gh = make({("DELETE", "repos/me/app/issues/7/labels/b"): 404})
    gh.labels("me/app", 7, [], ["b"])


def test_mark_ready_uses_graphql():
    gh = make({("GET", "repos/me/app/pulls/7"): {"node_id": "PR_x"}, ("POST", "graphql"): {"data": {}}})
    assert gh.mark_ready("me/app", 7) is None
    (m1, p1, b1), (m2, p2, b2) = gh._opener.calls
    assert (m1, p1, b1) == ("GET", "repos/me/app/pulls/7", None)
    assert (m2, p2) == ("POST", "graphql")
    assert "markPullRequestReadyForReview" in b2["query"]
    assert b2["variables"] == {"id": "PR_x"}


def test_graphql_returns_data_and_refuses_errors():
    gh = make({("POST", "graphql"): {"data": {"a": 1}}})
    assert gh.graphql("query { a }", {"x": 1}) == {"a": 1}
    assert gh._opener.calls == [("POST", "graphql", {"query": "query { a }", "variables": {"x": 1}})]
    gh = make({("POST", "graphql"): {"errors": [{"message": "nope"}]}})
    with pytest.raises(Refused, match="nope"):
        gh.graphql("query { a }", {})


def test_open_pull():
    path = "repos/me/app/pulls?state=open&head=me%3Afeat&per_page=1"
    gh = make({("GET", path): [{"html_url": URL}]})
    assert gh.open_pull("me/app", "feat") == URL
    assert gh._opener.calls == [("GET", path, None)]
    gh = make({("GET", path): []})
    assert gh.open_pull("me/app", "feat") is None


def test_every_post_sends_a_json_body():
    gh = make()
    gh.run_action("me/app", 5, "rerun")
    ((method, _, body),) = gh._opener.calls
    assert method == "POST" and body == {}


def test_run_is_read():
    gh = make({("GET", "repos/me/app/actions/runs/5"): {"id": 5, "head_branch": "feat"}})
    assert gh.run("me/app", 5)["head_branch"] == "feat"
    assert gh._opener.calls == [("GET", "repos/me/app/actions/runs/5", None)]


def test_refusal_carries_githubs_message():
    gh = make({("POST", "repos/me/app/issues/7/comments"): (422, "Validation Failed", {})})
    with pytest.raises(FromGitHub, match="422: Validation Failed"):
        gh.comment("me/app", 7, "x")


def test_githubs_message_is_scrubbed_and_cut():
    gh = make({("POST", "repos/me/app/issues/7/comments"): (422, "tok leaked\n\x1b[2J" + "y" * 900, {})})
    with pytest.raises(FromGitHub) as e:
        gh.comment("me/app", 7, "x")
    text = str(e.value)
    assert "tok leaked" not in text and "[token] leaked" in text
    assert "\n" not in text and "\x1b" not in text
    assert text.count("y") <= 500


def test_validation_errors_are_named():
    class Opener(FakeOpener):
        def open(self, req, timeout=None):
            body = {"message": "Validation Failed", "errors": [{"message": "A pull request already exists"}]}
            raise urllib.error.HTTPError(req.full_url, 422, "status", {}, io.BytesIO(json.dumps(body).encode()))

    gh = GitHub("tok")
    gh._opener = Opener()
    with pytest.raises(FromGitHub, match="Validation Failed.*A pull request already exists"):
        gh.create_pull("me/app", "feat", "main", "t", "b", False)


def test_graphql_errors_are_from_github():
    gh = make({("POST", "graphql"): {"errors": [{"message": "nope"}]}})
    with pytest.raises(FromGitHub, match="nope"):
        gh.graphql("query { a }", {})


@pytest.mark.parametrize(
    ("name", "path"),
    [
        ("v1.2.0", "repos/me/app/git/ref/tags/v1%2E2%2E0"),
        ("release/v1", "repos/me/app/git/ref/tags/release/v1"),
        ("a b#c?d", "repos/me/app/git/ref/tags/a%20b%23c%3Fd"),
    ],
)
def test_tag_exists(name, path):
    gh = make({("GET", path): {"ref": f"refs/tags/{name}"}})
    assert gh.tag_exists("me/app", name) is True
    assert gh._opener.calls == [("GET", path, None)]
    gh = make({("GET", path): 404})
    assert gh.tag_exists("me/app", name) is False


@pytest.mark.parametrize("status", [500, 403, 401])
def test_tag_lookup_failure_raises(status):
    gh = make({("GET", "repos/me/app/git/ref/tags/v1"): status})
    with pytest.raises(Refused, match=str(status)):
        gh.tag_exists("me/app", "v1")


@pytest.mark.parametrize("name", ["", "..", "a/../b", "a//b", "a/."])
def test_tag_lookup_refuses_dot_segments(name):
    gh = make()
    with pytest.raises(Refused, match="segment"):
        gh.tag_exists("me/app", name)
    assert gh._opener.calls == []


# --- rate limits ---------------------------------------------------------------------------------

RUN5 = ("GET", "repos/me/app/actions/runs/5")


@pytest.mark.parametrize(
    ("status", "message", "headers", "wait"),
    [
        (
            403,
            "API rate limit exceeded for user ID 1.",
            {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1100"},
            100,
        ),
        (403, "You have exceeded a secondary rate limit. Please wait.", {"Retry-After": "30"}, 30),
        (429, "You have exceeded a secondary rate limit.", {}, None),
        (
            403,
            "API rate limit exceeded",
            {"retry-after": "7", "x-ratelimit-remaining": "0", "x-ratelimit-reset": "1100"},
            7,
        ),
    ],
)
def test_rate_limit_is_its_own_refusal(status, message, headers, wait):
    gh = make({RUN5: (status, message, headers)})
    with pytest.raises(RateLimited) as e:
        gh.run("me/app", 5)
    assert isinstance(e.value, FromGitHub) and e.value.status == status
    assert f"returned {status}: {message}" in str(e.value)
    assert e.value.wait(now=1000.0) == wait


def test_a_429_without_a_body_is_a_rate_limit():
    gh = make({RUN5: 429})
    with pytest.raises(RateLimited, match="429"):
        gh.run("me/app", 5)


@pytest.mark.parametrize(
    ("message", "headers"),
    [
        ("Resource not accessible by personal access token", {}),
        ("Resource protected by organization SAML enforcement.", {"x-ratelimit-remaining": "4000"}),
    ],
)
def test_other_403s_are_not_rate_limits(message, headers):
    gh = make({RUN5: (403, message, headers)})
    with pytest.raises(GitHubError) as e:
        gh.run("me/app", 5)
    assert not isinstance(e.value, RateLimited)
    assert e.value.status == 403 and isinstance(e.value, FromGitHub)


def test_an_exhausted_quota_on_a_good_answer_is_remembered():
    reply = FakeResponse(b'{"id": 5}', {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1200"})
    gh = make({RUN5: reply})
    assert gh.rate_wait(now=1000.0) == 0
    gh.run("me/app", 5)
    assert gh.rate_wait(now=1000.0) == 200
    assert gh.rate_wait(now=1300.0) == 0
    gh._opener.replies[RUN5] = FakeResponse(
        b'{"id": 5}', {"X-RateLimit-Remaining": "4999", "X-RateLimit-Reset": "1200"}
    )
    gh.run("me/app", 5)
    assert gh.rate_wait(now=1000.0) == 0


# --- job logs ------------------------------------------------------------------------------------

LOGS = ("GET", "repos/me/app/actions/jobs/9/logs")
BLOB = "https://results.example.net/logs/9.txt?sig=abc"


class FakeDownloads:
    """Stands in for the opener of token-less downloads: answers in turn, records each request."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def open(self, req, timeout=None):
        self.calls.append((req.full_url, {k.lower(): v for k, v in req.header_items()}))
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def logs(*answers, api=None):
    gh = make({LOGS: api if api is not None else Redirected("redirected", BLOB)})
    gh._downloads = FakeDownloads(*answers)
    return gh


def test_cross_host_redirect_names_its_target():
    req = urllib.request.Request("https://api.github.com/repos/me/app/actions/jobs/9/logs")
    with pytest.raises(Redirected) as e:
        _SameHostRedirects().redirect_request(req, None, 302, "Found", {}, BLOB)
    assert e.value.url == BLOB and isinstance(e.value, FromGitHub)
    assert "not following" in str(e.value)


def test_job_log_follows_the_redirect_without_the_token():
    gh = logs(FakeResponse(b"line 1\nline 2\n", {"Content-Length": "14"}))
    assert gh.job_log("me/app", 9) == (b"line 1\nline 2\n", None)
    assert gh._opener.calls == [("GET", "repos/me/app/actions/jobs/9/logs", None)]
    ((url, headers),) = gh._downloads.calls
    assert url == BLOB
    assert "authorization" not in headers and "tok" not in str(headers)


def test_a_long_log_is_read_from_its_end():
    size = MAX_LOG + 1000
    tail = b"x" * (MAX_LOG - 4) + b"end\n"
    gh = logs(FakeResponse(b"", {"Content-Length": str(size)}), FakeResponse(tail, {}, status=206))
    data, note = gh.job_log("me/app", 9)
    assert data == tail and "last 2 MiB" in note
    first, second = gh._downloads.calls
    assert "range" not in first[1]
    assert second == (BLOB, second[1]) and second[1]["range"] == f"bytes={size - MAX_LOG}-"


def test_a_long_log_whose_host_ignores_ranges_gives_its_start():
    size = MAX_LOG * 3
    gh = logs(FakeResponse(b"", {"Content-Length": str(size)}), FakeResponse(b"y" * size, {}, status=200))
    data, note = gh.job_log("me/app", 9)
    assert len(data) == MAX_LOG and "start" in note


def test_a_log_without_a_length_is_capped():
    gh = logs(FakeResponse(b"z" * (MAX_LOG + 5), {}))
    data, note = gh.job_log("me/app", 9)
    assert len(data) == MAX_LOG and "start" in note
    assert len(gh._downloads.calls) == 1


def test_a_log_github_answers_itself_is_capped():
    gh = logs(api=FakeResponse(b"w" * (MAX_LOG + 1)))
    data, note = gh.job_log("me/app", 9)
    assert len(data) == MAX_LOG and "start" in note
    gh = logs(api=FakeResponse(b"short\n"))
    assert gh.job_log("me/app", 9) == (b"short\n", None)


def test_a_log_redirect_to_plain_http_is_refused():
    gh = logs(api=Redirected("redirected", "http://results.example.net/9.txt"))
    with pytest.raises(Refused, match="https"):
        gh.job_log("me/app", 9)
    assert gh._downloads.calls == []


@pytest.mark.parametrize(
    "error", [urllib.error.HTTPError(BLOB, 403, "Forbidden", {}, None), urllib.error.URLError("down")]
)
def test_a_failed_log_download_refuses(error):
    gh = logs(error)
    with pytest.raises(Refused, match="log"):
        gh.job_log("me/app", 9)


def test_a_missing_log_is_githubs_answer():
    gh = logs(api=(410, "Gone", {}))
    with pytest.raises(GitHubError) as e:
        gh.job_log("me/app", 9)
    assert e.value.status == 410
