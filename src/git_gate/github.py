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
MAX_LOG = 2 << 20  # bytes of a job log the gate downloads
PER_PAGE = 100
MAX_PAGES = 10  # pages of one list the gate reads: at most PER_PAGE * MAX_PAGES entries


class FromGitHub(Refused):
    """A refusal whose message carries text GitHub sent, which the agent must treat as data."""


class GitHubError(FromGitHub):
    """GitHub answered an error `status`; `headers` are the answer's, for callers that act on them."""

    def __init__(self, text: str, status: int, headers):
        super().__init__(text)
        self.status = status
        self.headers = headers


class RateLimited(GitHubError):
    """GitHub refused the call under its primary or secondary rate limit."""

    def wait(self, now: float) -> float | None:
        """Seconds GitHub asks the caller to wait: Retry-After, else until x-ratelimit-reset when the
        quota is spent; None when it names neither."""
        after = _number(_header(self.headers, "retry-after"))
        if after is not None:
            return max(0.0, after)
        return _reset_wait(self.headers, now)


_START = "the log is longer than {} MiB and its end could not be read; this is its start"


def _header(headers, name: str) -> str | None:
    """A header by its name in any case; `headers` is an HTTPMessage, a dict or None."""
    for key, value in (headers or {}).items():
        if key.lower() == name:
            return value
    return None


def _number(text: str | None) -> float | None:
    try:
        return float(text) if text is not None else None
    except ValueError:
        return None


def _reset_wait(headers, now: float) -> float | None:
    """Seconds until x-ratelimit-reset when x-ratelimit-remaining is 0; None otherwise."""
    if _header(headers, "x-ratelimit-remaining") != "0":
        return None
    reset = _number(_header(headers, "x-ratelimit-reset"))
    return None if reset is None else max(0.0, reset - now)


def _rate_limited(status: int, message: str, headers) -> bool:
    """A 429, or a 403 whose message or spent quota says it is GitHub's primary or secondary rate limit."""
    return status == 429 or (
        status == 403 and ("rate limit" in message.lower() or _header(headers, "x-ratelimit-remaining") == "0")
    )


def _plan_limit(message: str) -> bool:
    """True when a 403 says the plan cannot use the feature, rather than that the call was blocked."""
    return "Upgrade to GitHub Pro" in message or "make this repository public" in message.lower()


class Redirected(FromGitHub):
    """GitHub redirected to another host, `url`, which the gate does not follow with the token."""

    def __init__(self, text: str, url: str):
        super().__init__(text)
        self.url = url


class _SameHostRedirects(urllib.request.HTTPRedirectHandler):
    """urllib keeps the Authorization header across redirects, so refuse any that change host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            raise Redirected(f"GitHub redirected to another host ({newurl}); not following.", newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _HttpsRedirects(urllib.request.HTTPRedirectHandler):
    """For downloads, which carry no token: follow redirects, but only to https."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise Refused("The log download redirected to an address that is not https; not following.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class GitHub:
    def __init__(self, token: str, api: str = "https://api.github.com", timeout: int = 30):
        self.token = token
        self.api = api.rstrip("/")
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_SameHostRedirects)
        self._downloads = urllib.request.build_opener(_HttpsRedirects)  # never sent the token
        self._reset: float | None = None  # x-ratelimit-reset of the last answer that spent the quota

    def rate_wait(self, now: float) -> float:
        """Seconds until the quota resets when the last answer said it is spent; else 0."""
        return max(0.0, self._reset - now) if self._reset is not None else 0.0

    def _saw(self, headers) -> None:
        spent = _header(headers, "x-ratelimit-remaining") == "0"
        self._reset = _number(_header(headers, "x-ratelimit-reset")) if spent else None

    def _request(
        self,
        method: str,
        path: str,
        data: dict | None = None,
        absent: tuple[int, ...] = (),
        absent_if: Callable[[int, str], bool] | None = None,
        raw: int | None = None,
    ):
        """JSON in and out. Statuses in `absent`, or an error `absent_if(status, message)` accepts, give None.
        With `raw`, the answer is returned as bytes, at most `raw` + 1 of them, and not parsed.

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
                self._saw(getattr(resp, "headers", None))
                body = resp.read() if raw is None else resp.read(raw + 1)
        except urllib.error.HTTPError as e:
            self._saw(e.headers)
            message, detail = self._message(e)
            if e.code in absent or (absent_if and absent_if(e.code, message)):
                return None
            said = self._clean("; ".join(m for m in (message, *detail) if m))
            if _rate_limited(e.code, message, e.headers):
                text = f"GitHub {method} {path} returned {e.code}" + (f": {said}" if said else ".")
                raise RateLimited(text, e.code, e.headers) from None
            if said:
                raise GitHubError(f"GitHub {method} {path} returned {e.code}: {said}", e.code, e.headers) from None
            raise Refused(f"GitHub {method} {path} returned {e.code}.") from None
        except urllib.error.URLError as e:
            raise Refused(f"GitHub {method} {path} failed: {e.reason}.") from None
        if raw is not None:
            return body
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

    def tag_exists(self, repo: str, name: str) -> bool:
        """Whether `repo` has the tag `name`. Each `/`-separated part is quoted as a segment of its
        own, so a tag like release/v1 keeps its path and no part can become `.` or `..`. Only a 404
        means "no tag"; any other failure raises."""
        path = "/".join(self._segment(part) for part in name.split("/"))
        return self._request("GET", f"repos/{repo}/git/ref/tags/{path}", absent=(404,)) is not None

    def _pages(self, path: str, key: str) -> list:
        """The `key` list of a paged answer such as {"total_count": n, key: [...]}, page by page,
        at most MAX_PAGES of them."""
        items: list = []
        sep = "&" if "?" in path else "?"
        for page in range(1, MAX_PAGES + 1):
            data = self._request("GET", f"{path}{sep}per_page={PER_PAGE}&page={page}")
            data = data if isinstance(data, dict) else {}
            got = data.get(key) if isinstance(data.get(key), list) else []
            items += got
            total = data.get("total_count")
            if len(got) < PER_PAGE or (isinstance(total, int) and len(items) >= total):
                break
        return items

    def pull(self, repo: str, number: int) -> dict | None:
        """A pull request, or None when `repo` has none numbered `number`."""
        return self._request("GET", f"repos/{repo}/pulls/{number}", absent=(404,))

    def commit(self, repo: str, sha: str) -> dict | None:
        """A commit, or None when GitHub knows no commit by that (abbreviated) SHA."""
        return self._request("GET", f"repos/{repo}/commits/{self._segment(sha)}", absent=(404, 422))

    def check_runs(self, repo: str, sha: str) -> list[dict]:
        """Every check run of commit `sha`, the latest of each name."""
        return self._pages(f"repos/{repo}/commits/{self._segment(sha)}/check-runs?filter=latest", "check_runs")

    def statuses(self, repo: str, sha: str) -> list[dict]:
        """The latest commit status of each context on commit `sha`."""
        return self._pages(f"repos/{repo}/commits/{self._segment(sha)}/status", "statuses")

    def find_run(self, repo: str, run_id: int) -> dict | None:
        """A workflow run, or None when `repo` has none with that id."""
        return self._request("GET", f"repos/{repo}/actions/runs/{run_id}", absent=(404,))

    def jobs(self, repo: str, run_id: int) -> list[dict]:
        """The jobs of a workflow run's latest attempt."""
        return self._pages(f"repos/{repo}/actions/runs/{run_id}/jobs?filter=latest", "jobs")

    def run(self, repo: str, run_id: int) -> dict:
        """A workflow run, which says among other things which branch it ran on (`head_branch`)."""
        return self._request("GET", f"repos/{repo}/actions/runs/{run_id}")

    def job_log(self, repo: str, job_id: int, cap: int = MAX_LOG) -> tuple[bytes, str | None]:
        """A job's log as bytes, at most `cap` of them, and a note when that is not the whole log.

        GitHub answers with a redirect to a signed download address on another host. That address
        is fetched without the token. A log longer than `cap` is fetched again from `cap` bytes
        before its end, since its end is where a failure shows; a host that ignores the range
        leaves only its start.
        """
        try:
            data = self._request("GET", f"repos/{repo}/actions/jobs/{job_id}/logs", raw=cap)
        except Redirected as e:
            return self._download(e.url, cap)
        return data[:cap], _START.format(cap >> 20) if len(data) > cap else None

    def _download(self, url: str, cap: int) -> tuple[bytes, str | None]:
        if urllib.parse.urlsplit(url).scheme != "https":
            raise Refused("GitHub sent the log to an address that is not https; not following.")

        def get(span: str | None = None):
            headers = {"User-Agent": "git-gate", **({"Range": span} if span else {})}
            return self._downloads.open(urllib.request.Request(url, headers=headers), timeout=self.timeout)

        try:
            with get() as resp:
                size = _number(_header(resp.headers, "content-length"))
                if size is None or size <= cap:
                    data = resp.read(cap + 1)
                    return data[:cap], _START.format(cap >> 20) if len(data) > cap else None
            with get(f"bytes={int(size) - cap}-") as resp:
                data = resp.read(cap)
                if getattr(resp, "status", 200) == 206:
                    return data, f"the log is {size / (1 << 20):.1f} MiB; the gate read its last {cap >> 20} MiB"
                return data, _START.format(cap >> 20)
        except urllib.error.HTTPError as e:
            raise Refused(f"Downloading the log failed with status {e.code}.") from None
        except OSError as e:
            raise Refused(f"Downloading the log failed: {getattr(e, 'reason', None) or e}.") from None

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
