"""Sign in to GitHub through the OAuth device flow, for the user's own GitHub App."""

import contextlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field

from git_gate import Refused
from git_gate.github import _SameHostRedirects

CODE_URL = "https://github.com/login/device/code"
TOKEN_URL = "https://github.com/login/oauth/access_token"
REVOKE_URL = "https://api.github.com/credentials/revoke"
GRANT = "urn:ietf:params:oauth:grant-type:device_code"
TIMEOUT = 30
SNIPPET = 120
CHECKS = 6
CHECK_GAP = 5
REVOKED = "Signed out: GitHub revoked this run's token."
NOT_CONFIRMED = (
    "Warning: GitHub has not confirmed this run's token is revoked. "
    "Revoke it at https://github.com/settings/apps/authorizations"
)
EXPIRED = "The sign-in code expired before it was approved. Run the command again."


def _snippet(raw: bytes) -> str:
    text = raw[:SNIPPET].decode("utf-8", "replace")
    return "".join(c if c.isprintable() else " " for c in text).strip()


def _post(url: str, form: dict) -> dict:
    """A form POST to github.com that asks for JSON; redirects to another host are refused."""
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(form).encode(),
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    opener = urllib.request.build_opener(_SameHostRedirects)
    try:
        with opener.open(request, timeout=TIMEOUT) as answer:
            raw = answer.read(1 << 20)
    except urllib.error.HTTPError as e:
        raise Refused(f"GitHub answered HTTP {e.code} during sign-in: {_snippet(e.read(SNIPPET))}") from None
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise Refused(f"Could not reach GitHub to sign in ({type(e).__name__}).") from None
    try:
        data = json.loads(raw)
    except ValueError:
        raise Refused(f"GitHub did not answer sign-in with JSON: {_snippet(raw)}") from None
    if not isinstance(data, dict):
        raise Refused("GitHub answered sign-in with something unexpected.")
    return data


def _post_json(url: str, body: dict) -> int:
    """A JSON POST without any Authorization (GitHub refuses an authenticated revoke); returns the status."""
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Accept": "application/vnd.github+json", "Content-Type": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(_SameHostRedirects)
    try:
        with opener.open(request, timeout=TIMEOUT) as answer:
            answer.read(1 << 20)
            return answer.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, OSError) as e:
        raise Refused(f"Could not reach GitHub to revoke ({type(e).__name__}).") from None


@dataclass(frozen=True)
class Login:
    """What a completed sign-in holds: the user token and, when GitHub sent one, its refresh token."""

    token: str = field(repr=False)
    refresh: str | None = field(repr=False)


def revoke(
    login: Login,
    *,
    alive: Callable[[str], bool],
    post: Callable[[str, dict], int] = _post_json,
    sleep: Callable[[float], None] = time.sleep,
    say: Callable[[str], None],
) -> bool:
    """Revoke the run's tokens at GitHub, then confirm the access token is dead; say how it went. When
    GitHub refuses the access and refresh tokens together, the access token is sent alone once more."""
    credentials = [login.token] + ([login.refresh] if login.refresh else [])
    try:
        status = post(REVOKE_URL, {"credentials": credentials})
        if status != 202 and login.refresh:  # GitHub may refuse the pair: the access token alone, once more
            status = post(REVOKE_URL, {"credentials": [login.token]})
        if status != 202:
            say(f"GitHub refused the revocation ({status}).")
        else:
            for _ in range(CHECKS):
                sleep(CHECK_GAP)
                if not alive(login.token):
                    say(REVOKED)
                    return True
    except Refused as e:
        say(str(e))
    except KeyboardInterrupt:
        say("")
    except Exception as e:  # whatever the check hit: the text may echo a token, so only the class is shown
        say(f"Could not confirm the revocation ({type(e).__name__}).")
    say(NOT_CONFIRMED)
    return False


def _number(value, default: float) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else default


def device_login(
    client_id: str,
    *,
    say: Callable[[str], None],
    open_url: Callable[[str], object] = webbrowser.open,
    post: Callable[[str, dict], dict] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Login:
    """Run the device flow and return the user's tokens; raise Refused if it does not complete."""
    post = post or _post
    first = post(CODE_URL, {"client_id": client_id})
    if "error" in first:
        raise Refused(f"GitHub refused the sign-in: {first['error']}.")
    device_code, user_code, uri = (first.get(k) for k in ("device_code", "user_code", "verification_uri"))
    if not all(isinstance(v, str) and v for v in (device_code, user_code, uri)):
        raise Refused("GitHub's sign-in answer was incomplete.")
    interval = _number(first.get("interval"), 5.0)
    deadline = clock() + _number(first.get("expires_in"), 900.0)
    say(f"Open {uri} and enter the code {user_code}.")
    say("Waiting for approval...")
    with contextlib.suppress(webbrowser.Error, OSError):
        open_url(uri)
    try:
        while True:
            sleep(interval)
            if clock() >= deadline:
                raise Refused(EXPIRED)
            answer = post(TOKEN_URL, {"client_id": client_id, "device_code": device_code, "grant_type": GRANT})
            token = answer.get("access_token")
            if isinstance(token, str) and token:
                refresh = answer.get("refresh_token")
                return Login(token, refresh if isinstance(refresh, str) and refresh else None)
            error = answer.get("error")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval = max(interval + 5, _number(answer.get("interval"), 0.0))
            elif error == "expired_token":
                raise Refused(EXPIRED)
            elif error == "access_denied":
                raise Refused("The sign-in was declined on GitHub.")
            else:
                raise Refused(f"GitHub refused the sign-in: {str(error)[:60] or 'no token and no error'}.")
    except KeyboardInterrupt:
        raise Refused("Sign-in cancelled.") from None
