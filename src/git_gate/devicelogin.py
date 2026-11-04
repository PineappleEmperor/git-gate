"""Sign in to GitHub through the OAuth device flow, for the user's own GitHub App."""

import contextlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable

from git_gate import Refused
from git_gate.github import _SameHostRedirects

CODE_URL = "https://github.com/login/device/code"
TOKEN_URL = "https://github.com/login/oauth/access_token"
GRANT = "urn:ietf:params:oauth:grant-type:device_code"
TIMEOUT = 30
SNIPPET = 120
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
) -> str:
    """Run the device flow and return the user access token; raise Refused if it does not complete."""
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
                return token
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
