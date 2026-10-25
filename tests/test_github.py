import io
import json
import urllib.error

import pytest

from git_gate import Refused
from git_gate.github import GitHub

API = "https://api.github.com/"
URL = "https://github.com/me/app/pull/7"


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Stands in for the urllib opener: records each request, answers from canned replies."""

    def __init__(self, replies=None):
        self.replies = replies or {}
        self.calls = []

    def open(self, req, timeout=None):
        path = req.full_url.removeprefix(API)
        body = json.loads(req.data) if req.data else None
        self.calls.append((req.get_method(), path, body))
        reply = self.replies.get((req.get_method(), path), {"html_url": URL})
        if isinstance(reply, int):
            raise urllib.error.HTTPError(req.full_url, reply, "status", {}, None)
        return FakeResponse(json.dumps(reply).encode())


def make(replies=None):
    gh = GitHub("tok")
    gh._opener = FakeOpener(replies)
    return gh


def test_rules_403_means_no_rules():
    gh = make({("GET", "repos/me/app/rules/branches/main"): 403})
    assert gh.rule_types("me/app", "main") == set()


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
        lambda g: g.request_reviewers("me/app", 7, ["r"]),
        [("POST", "repos/me/app/pulls/7/requested_reviewers", {"reviewers": ["r"]})],
    ),
    (
        lambda g: g.run_action("me/app", 5, "rerun"),
        [("POST", "repos/me/app/actions/runs/5/rerun", None)],
    ),
    (
        lambda g: g.run_action("me/app", 5, "rerun_failed"),
        [("POST", "repos/me/app/actions/runs/5/rerun-failed-jobs", None)],
    ),
    (
        lambda g: g.run_action("me/app", 5, "cancel"),
        [("POST", "repos/me/app/actions/runs/5/cancel", None)],
    ),
    (
        lambda g: g.label("me/app", "create", "bug", color="f00", description="d"),
        [("POST", "repos/me/app/labels", {"name": "bug", "color": "f00", "description": "d"})],
    ),
    (
        lambda g: g.label("me/app", "edit", "bug", new_name="defect"),
        [("PATCH", "repos/me/app/labels/bug", {"new_name": "defect"})],
    ),
    (
        lambda g: g.label("me/app", "delete", "a/b c"),
        [("DELETE", "repos/me/app/labels/a%2Fb%20c", None)],
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


def test_label_rejects_unknown_action():
    gh = make()
    with pytest.raises(Refused):
        gh.label("me/app", "rename", "bug")
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
