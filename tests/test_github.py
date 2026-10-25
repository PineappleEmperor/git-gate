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
