"""Register the user's private setup GitHub App once, through GitHub's app-manifest flow."""

import contextlib
import dataclasses
import html
import http.server
import json
import os
import secrets
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable
from pathlib import Path

from git_gate import Refused
from git_gate.config import config_dir
from git_gate.devicelogin import TIMEOUT, _snippet
from git_gate.github import _SameHostRedirects

CONVERT_URL = "https://api.github.com/app-manifests/{code}/conversions"
NEW_APP_URL = "https://github.com/settings/apps/new"
FILE = "setup-app.json"
WAIT = 600.0
NOT_REGISTERED = "No setup app registered; run: gitgatesetup --register"


@dataclasses.dataclass(frozen=True)
class AppConfig:
    client_id: str
    slug: str
    owner: str


def load_app(cfg: Path | None = None) -> AppConfig:
    path = (cfg or config_dir()) / FILE
    try:
        raw = path.read_text()
    except FileNotFoundError:
        raise Refused(NOT_REGISTERED) from None
    except OSError as e:
        raise Refused(f"Cannot read {path}: {e.strerror}.") from None
    try:
        data = json.loads(raw)
    except ValueError:
        raise Refused(f"{path} is not valid JSON; delete it and run: gitgatesetup --register") from None
    values = [data.get(k) if isinstance(data, dict) else None for k in ("client_id", "slug", "owner")]
    if not all(isinstance(v, str) and v for v in values):
        raise Refused(f"{path} lacks client_id, slug or owner; delete it and run: gitgatesetup --register")
    return AppConfig(*values)


def manifest(name: str, redirect_url: str) -> dict:
    return {
        "name": name,
        "url": "https://github.com/PineappleEmperor/git-gate",
        "redirect_url": redirect_url,
        "public": False,
        "default_permissions": {"administration": "write", "metadata": "read"},
        "default_events": [],
        "request_oauth_on_install": False,
    }


def _convert(code: str) -> dict:
    """Exchange the manifest code for the app's credentials; no auth, empty body, JSON answer."""
    url = CONVERT_URL.format(code=urllib.parse.quote(code, safe=""))
    request = urllib.request.Request(url, data=b"", headers={"Accept": "application/json"}, method="POST")
    opener = urllib.request.build_opener(_SameHostRedirects)
    try:
        with opener.open(request, timeout=TIMEOUT) as answer:
            raw = answer.read(1 << 20)
    except urllib.error.HTTPError as e:
        raise Refused(f"GitHub answered HTTP {e.code} registering the app: {_snippet(e.read(120))}") from None
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise Refused(f"Could not reach GitHub to register the app ({type(e).__name__}).") from None
    try:
        data = json.loads(raw)
    except ValueError:
        raise Refused(f"GitHub did not answer registration with JSON: {_snippet(raw)}") from None
    if not isinstance(data, dict):
        raise Refused("GitHub answered registration with something unexpected.")
    return data


def _form_html(manifest_json: str, state: str) -> str:
    action = f"{NEW_APP_URL}?state={urllib.parse.quote(state, safe='')}"
    return (
        "<!doctype html><meta charset=utf-8><title>git-gate setup</title>"
        f'<form id=f method=post action="{html.escape(action)}">'
        f'<input type=hidden name=manifest value="{html.escape(manifest_json)}">'
        "<button type=submit>Continue to GitHub</button></form>"
        "<script>document.getElementById('f').submit()</script>"
    )


def serve(
    form_for: Callable[[int], str],
    state: str,
    *,
    ready: Callable[[int], None] | None = None,
    timeout: float = WAIT,
) -> str:
    """Serve the form on 127.0.0.1, wait for GitHub's redirect to /done and return its code."""
    outcome: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        timeout = 5  # an idle connection must not hold a thread forever

        def _send(self, status: int, text: str) -> None:
            raw = text.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            url = urllib.parse.urlsplit(self.path)
            if url.path == "/":
                self._send(200, form_for(self.server.server_port))
            elif url.path == "/done":
                query = urllib.parse.parse_qs(url.query)
                got = (query.get("state") or [""])[0]
                code = (query.get("code") or [""])[0]
                if not secrets.compare_digest(got.encode(), state.encode()):
                    outcome["error"] = "The redirect from GitHub carried the wrong state; nothing was registered."
                    self._send(400, "Refused: wrong state.")
                elif not code:
                    outcome["error"] = "The redirect from GitHub carried no code; nothing was registered."
                    self._send(400, "Refused: no code.")
                else:
                    outcome["code"] = code
                    self._send(200, "<!doctype html><title>git-gate</title>You can close this tab.")
            else:
                self._send(404, "Not found")

        def log_message(self, *args) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True  # an idle connection never blocks the loop or server_close
    server.timeout = 0.2
    try:
        if ready:
            ready(server.server_port)
        deadline = time.monotonic() + timeout
        while not outcome:
            if time.monotonic() >= deadline:
                waited = f"{timeout / 60:g} minutes" if timeout >= 60 else f"{timeout:g} seconds"
                raise Refused(f"Gave up waiting for GitHub after {waited}; run the command again.")
            server.handle_request()
    except KeyboardInterrupt:
        raise Refused("Registration cancelled.") from None
    finally:
        server.server_close()
    if "error" in outcome:
        raise Refused(outcome["error"])
    return outcome["code"]


def _write(path: Path, app: AppConfig) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".setup-app-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(dataclasses.asdict(app), f)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def register(
    *,
    say: Callable[[str], None],
    open_url: Callable[[str], object] = webbrowser.open,
    convert: Callable[[str], dict] | None = None,
    serve: Callable[..., str] = serve,
    cfg: Path | None = None,
) -> AppConfig:
    """Register the app once and keep only its public identifiers."""
    directory = cfg or config_dir()
    path = directory / FILE
    if path.exists():
        slug = "unknown"
        with contextlib.suppress(Refused):
            slug = load_app(directory).slug
        raise Refused(f"A setup app is already registered ({slug}); delete {path} to register another.")
    convert = convert or _convert
    state = secrets.token_urlsafe(24)
    name = f"git-gate setup {secrets.token_hex(3)}"

    def form_for(port: int) -> str:
        return _form_html(json.dumps(manifest(name, f"http://127.0.0.1:{port}/done")), state)

    def ready(port: int) -> None:
        local = f"http://127.0.0.1:{port}/"
        say("Opening GitHub to create the app…")
        say(f"If the browser does not open, visit {local}")
        with contextlib.suppress(webbrowser.Error, OSError):
            open_url(local)

    code = serve(form_for, state, ready=ready)
    body = convert(code)
    owner = body.get("owner")
    values = [body.get("client_id"), body.get("slug"), owner.get("login") if isinstance(owner, dict) else None]
    if not all(isinstance(v, str) and v for v in values):
        raise Refused("GitHub's registration answer was incomplete; nothing was saved.")
    app = AppConfig(*values)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write(path, app)
    install = f"https://github.com/apps/{app.slug}/installations/new"
    say(f"Registered {app.slug}.")
    say(f"Install it on All repositories: {install}")
    with contextlib.suppress(webbrowser.Error, OSError):
        open_url(install)
    say(f"Then make sure 'Enable Device Flow' is ticked at https://github.com/settings/apps/{app.slug} (once).")
    return app
