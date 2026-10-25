"""The few GitHub REST calls the gate makes itself."""

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

from git_gate import Refused

PROTECTING_RULES = {
    "update",
    "deletion",
    "non_fast_forward",
    "pull_request",
    "required_status_checks",
    "required_deployments",
}


MAX_MESSAGE = 500


class FromGitHub(Refused):
    """A refusal whose message carries text GitHub sent, which the agent must treat as data."""


def _plan_limit(message: str) -> bool:
    """True when a 403 says the plan cannot use the feature, rather than that the call was blocked."""
    return "Upgrade to GitHub Pro" in message or "make this repository public" in message.lower()


class _SameHostRedirects(urllib.request.HTTPRedirectHandler):
    """urllib keeps the Authorization header across redirects, so refuse any that change host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            raise FromGitHub(f"GitHub redirected to another host ({newurl}); not following.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class GitHub:
    def __init__(self, token: str, api: str = "https://api.github.com", timeout: int = 30):
        self.token = token
        self.api = api.rstrip("/")
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_SameHostRedirects)

    def _request(
        self,
        method: str,
        path: str,
        data: dict | None = None,
        absent: tuple[int, ...] = (),
        absent_if: Callable[[int, str], bool] | None = None,
    ):
        """JSON in and out. Statuses in `absent`, or an error `absent_if(status, message)` accepts, give None.

        Every POST sends a JSON body, `{}` when there is nothing to say. A refusal names GitHub's
        error message, cleaned and cut to MAX_MESSAGE characters.
        """
        if data is None and method == "POST":
            data = {}
        req = urllib.request.Request(
            f"{self.api}/{path}",
            data=json.dumps(data).encode() if data is not None else None,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "git-gate",
                **({"Content-Type": "application/json"} if data is not None else {}),
            },
        )
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as e:
            message, detail = self._message(e)
            if e.code in absent or (absent_if and absent_if(e.code, message)):
                return None
            said = self._clean("; ".join(m for m in (message, *detail) if m))
            if said:
                raise FromGitHub(f"GitHub {method} {path} returned {e.code}: {said}") from None
            raise Refused(f"GitHub {method} {path} returned {e.code}.") from None
        except urllib.error.URLError as e:
            raise Refused(f"GitHub {method} {path} failed: {e.reason}.") from None
        return json.loads(body) if body else {}

    @staticmethod
    def _message(error: urllib.error.HTTPError) -> tuple[str, list[str]]:
        """GitHub's JSON error body: its `message` ("" when there is none) and the messages or codes
        of its `errors` list, which say what a 422 "Validation Failed" was about."""
        try:
            data = json.loads(error.read())
        except OSError, ValueError, AttributeError:
            return "", []
        if not isinstance(data, dict):
            return "", []
        errors = data.get("errors")
        detail = [
            str(e.get("message") or e.get("code") or "") if isinstance(e, dict) else str(e)
            for e in (errors if isinstance(errors, list) else [])
        ]
        return str(data.get("message") or ""), detail

    def _clean(self, text: str) -> str:
        """GitHub's text for a refusal: one line, no token, at most MAX_MESSAGE characters."""
        if self.token:
            text = text.replace(self.token, "[token]")
        text = "".join(c if c.isprintable() else " " for c in text).strip()
        return text if len(text) <= MAX_MESSAGE else text[: MAX_MESSAGE - 3] + "..."

    @staticmethod
    def _branch(branch: str) -> str:
        return urllib.parse.quote(branch, safe="/")

    def default_branch(self, repo: str) -> str:
        return self._request("GET", f"repos/{repo}")["default_branch"]

    def branch(self, repo: str, branch: str) -> dict | None:
        """{'sha': ..., 'protected': bool}, or None when the branch does not exist."""
        data = self._request("GET", f"repos/{repo}/branches/{self._branch(branch)}", absent=(404,))
        if data is None:
            return None
        return {"sha": data["commit"]["sha"], "protected": bool(data.get("protected"))}

    def rule_types(self, repo: str, branch: str) -> set[str]:
        # Free-plan private repos answer 403 "Upgrade to GitHub Pro": no rules there. Any other 403
        # (rate limit, SSO, missing permission) says nothing about the rules, so it must not read as "none".
        data = self._request(
            "GET",
            f"repos/{repo}/rules/branches/{self._branch(branch)}",
            absent=(404,),
            absent_if=lambda status, message: status == 403 and _plan_limit(message),
        )
        return {r.get("type", "") for r in data or []}

    def pulls_from(self, repo: str, branch: str) -> list[dict]:
        owner = repo.split("/")[0]
        head = urllib.parse.quote(f"{owner}:{branch}", safe="")
        data = self._request("GET", f"repos/{repo}/pulls?state=all&per_page=100&head={head}")
        return [
            {
                "number": p["number"],
                "state": p["state"],
                "closed_at": p.get("closed_at") or "",
                "head_sha": p["head"]["sha"],
            }
            for p in data
        ]

    def delete_branch(self, repo: str, branch: str) -> None:
        self._request("DELETE", f"repos/{repo}/git/refs/heads/{self._branch(branch)}")

    def graphql(self, query: str, variables: dict) -> dict:
        """The `data` of a GraphQL answer; GraphQL reports failures with status 200, so check `errors`."""
        out = self._request("POST", "graphql", {"query": query, "variables": variables})
        if out.get("errors"):
            messages = "; ".join(str(e.get("message", e)) if isinstance(e, dict) else str(e) for e in out["errors"])
            raise FromGitHub(f"GitHub GraphQL failed: {self._clean(messages)}")
        return out.get("data") or {}

    @staticmethod
    def _segment(value: str) -> str:
        """One path segment from user input. Dots are encoded and dot-only values refused, so a
        value can never turn into `.` or `..` and walk the request up out of its endpoint."""
        if not value or set(value) == {"."}:
            raise Refused(f"Refusing path segment {value!r}.")
        return urllib.parse.quote(value, safe="").replace(".", "%2E")

    def open_pull(self, repo: str, branch: str) -> str | None:
        head = urllib.parse.quote(f"{repo.split('/')[0]}:{branch}", safe="")
        data = self._request("GET", f"repos/{repo}/pulls?state=open&head={head}&per_page=1")
        return data[0]["html_url"] if data else None

    def comment(self, repo: str, number: int, body: str) -> str:
        return self._request("POST", f"repos/{repo}/issues/{number}/comments", {"body": body})["html_url"]

    def edit(
        self,
        repo: str,
        number: int,
        *,
        pull: bool,
        title: str | None = None,
        body: str | None = None,
        base: str | None = None,
        state: str | None = None,
    ) -> str:
        """Change only the fields given; `base` applies to pull requests only."""
        fields = {"title": title, "body": body, "base": base, "state": state}
        data = {k: v for k, v in fields.items() if v is not None}
        kind = "pulls" if pull else "issues"
        return self._request("PATCH", f"repos/{repo}/{kind}/{number}", data)["html_url"]

    def create_issue(self, repo: str, title: str, body: str) -> str:
        return self._request("POST", f"repos/{repo}/issues", {"title": title, "body": body})["html_url"]

    def create_pull(self, repo: str, head: str, base: str, title: str, body: str, draft: bool) -> str:
        data = {"head": head, "base": base, "title": title, "body": body, "draft": draft}
        return self._request("POST", f"repos/{repo}/pulls", data)["html_url"]

    def mark_ready(self, repo: str, number: int) -> None:
        """Take a draft out of draft; REST has no endpoint for it, so this goes through GraphQL."""
        node_id = self._request("GET", f"repos/{repo}/pulls/{number}")["node_id"]
        query = (
            "mutation($id: ID!) { markPullRequestReadyForReview(input: {pullRequestId: $id}) { pullRequest { id } } }"
        )
        self.graphql(query, {"id": node_id})

    def review(self, repo: str, number: int, event: str, body: str) -> str:
        if event not in ("COMMENT", "REQUEST_CHANGES"):
            raise Refused("Approving is the user's: a review here can only comment or request changes.")
        data = {"event": event, "body": body}
        return self._request("POST", f"repos/{repo}/pulls/{number}/reviews", data)["html_url"]

    def labels(self, repo: str, number: int, add: list[str], remove: list[str]) -> None:
        base = f"repos/{repo}/issues/{number}/labels"
        if add:
            self._request("POST", base, {"labels": add})
        for name in remove:
            self._request("DELETE", f"{base}/{self._segment(name)}", absent=(404,))

    def assignees(self, repo: str, number: int, add: list[str], remove: list[str]) -> None:
        path = f"repos/{repo}/issues/{number}/assignees"
        if add:
            self._request("POST", path, {"assignees": add})
        if remove:
            self._request("DELETE", path, {"assignees": remove})

    def request_reviewers(self, repo: str, number: int, add: list[str], remove: list[str]) -> None:
        path = f"repos/{repo}/pulls/{number}/requested_reviewers"
        if add:
            self._request("POST", path, {"reviewers": add})
        if remove:
            self._request("DELETE", path, {"reviewers": remove})

    def run(self, repo: str, run_id: int) -> dict:
        """A workflow run, which says among other things which branch it ran on (`head_branch`)."""
        return self._request("GET", f"repos/{repo}/actions/runs/{run_id}")

    def run_action(self, repo: str, run_id: int, action: str) -> None:
        endpoints = {"rerun": "rerun", "rerun_failed": "rerun-failed-jobs", "cancel": "cancel"}
        if action not in endpoints:
            raise Refused(f"Unknown run action {action!r}; use one of {', '.join(endpoints)}.")
        self._request("POST", f"repos/{repo}/actions/runs/{run_id}/{endpoints[action]}")

    def label(
        self,
        repo: str,
        action: str,
        name: str,
        color: str | None = None,
        description: str | None = None,
        new_name: str | None = None,
    ) -> None:
        """Create or edit a repository label definition; only the fields given are sent."""
        if action == "create":
            fields = {"name": name, "color": color, "description": description}
            self._request("POST", f"repos/{repo}/labels", {k: v for k, v in fields.items() if v is not None})
        elif action == "edit":
            fields = {"new_name": new_name, "color": color, "description": description}
            data = {k: v for k, v in fields.items() if v is not None}
            self._request("PATCH", f"repos/{repo}/labels/{self._segment(name)}", data)
        else:
            raise Refused(f"Unknown label action {action!r}; use create or edit.")
