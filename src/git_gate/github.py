"""The few GitHub REST calls the gate makes itself."""

import json
import urllib.error
import urllib.parse
import urllib.request

from git_gate import Refused

PROTECTING_RULES = {
    "update",
    "deletion",
    "non_fast_forward",
    "pull_request",
    "required_status_checks",
    "required_deployments",
}


class _SameHostRedirects(urllib.request.HTTPRedirectHandler):
    """urllib keeps the Authorization header across redirects, so refuse any that change host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            raise Refused(f"GitHub redirected to another host ({newurl}); not following.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class GitHub:
    def __init__(self, token: str, api: str = "https://api.github.com", timeout: int = 30):
        self.token = token
        self.api = api.rstrip("/")
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_SameHostRedirects)

    def _request(self, method: str, path: str, absent: tuple[int, ...] = ()):
        """Statuses listed in `absent` mean "nothing there" and return None."""
        req = urllib.request.Request(
            f"{self.api}/{path}",
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "git-gate",
            },
        )
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as e:
            if e.code in absent:
                return None
            raise Refused(f"GitHub {method} {path} returned {e.code}.") from None
        except urllib.error.URLError as e:
            raise Refused(f"GitHub {method} {path} failed: {e.reason}.") from None
        return json.loads(body) if body else {}

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
        # Free-plan private repos answer 403 ("Upgrade to GitHub Pro"): no rules there, not an error.
        data = self._request("GET", f"repos/{repo}/rules/branches/{self._branch(branch)}", absent=(403, 404))
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
