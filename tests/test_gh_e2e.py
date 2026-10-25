"""The real /usr/bin/gh against a fake local API, through Gate.dispatch (C1).

gh compiles --jq filters with gojq's environment loader, so `$ENV` and `env` read gh's own
environment. The token must not be recoverable from gh's output in any form: not plain, not
scrubbed to [token], not as code points, lower-cased or base64.
"""

import base64
import http.server
import json
import stat
import threading
from pathlib import Path

import pytest

from conftest import TOKEN
from git_gate import Refused, ghpolicy

GH = Path("/usr/bin/gh")
pytestmark = pytest.mark.skipif(not GH.is_file(), reason="needs the real gh at /usr/bin/gh")


class FakeAPI(http.server.BaseHTTPRequestHandler):
    """api.github.localhost: answers REST GETs and pr list's GraphQL query, records the auth header."""

    seen: list[tuple[str, str, str | None]] = []

    def _reply(self, obj) -> None:
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.seen.append(("GET", self.path, self.headers.get("Authorization")))
        self._reply({"full_name": "me/app", "x": 1})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.seen.append(("POST", self.path, self.headers.get("Authorization")))
        pulls = {"totalCount": 1, "nodes": [{"number": 7}], "pageInfo": {"hasNextPage": False, "endCursor": ""}}
        self._reply({"data": {"repository": {"pullRequests": pulls}}})

    def log_message(self, *args):
        pass


@pytest.fixture
def real_gh(env):
    """The gate runs the real gh; a wrapper only points it at the fake API on github.localhost."""
    FakeAPI.seen = []
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeAPI)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    wrapper = env.tmp / "gh-real"
    wrapper.write_text(
        "#!/bin/sh\n"
        "# Test-only: the gate's hosts file names github.com; the fake API is github.localhost.\n"
        'hosts="$GH_CONFIG_DIR/hosts.yml"\n'
        'test -f "$hosts" && sed -i "s/^github\\.com:/github.localhost:/" "$hosts"\n'
        'if [ -n "$GH_REPO" ]; then GH_REPO="github.localhost/$GH_REPO"; export GH_REPO; fi\n'
        f'exec env GH_HOST=github.localhost HTTP_PROXY=http://127.0.0.1:{httpd.server_port} {GH} "$@"\n'
    )
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    env.gate.gh_bin = str(wrapper)
    yield FakeAPI.seen
    httpd.shutdown()
    httpd.server_close()


def _token_forms() -> list[str]:
    codes = [ord(c) for c in TOKEN]
    return [
        TOKEN,
        TOKEN.lower(),
        TOKEN.upper(),
        TOKEN[::-1],
        "[token]",
        TOKEN[8:24],
        json.dumps(codes),
        json.dumps(codes, separators=(",", ":")),
        ",".join(map(str, codes)),
        base64.b64encode(TOKEN.encode()).decode(),
        TOKEN.encode().hex(),
    ]


def _assert_no_token(out: str) -> None:
    for form in _token_forms():
        assert form not in out, form


@pytest.mark.parametrize(
    "args",
    [
        ["api", "repos/{owner}/{repo}", "--jq", "$ENV.GH_TOKEN|explode|implode|ascii_downcase"],
        ["api", "repos/{owner}/{repo}", "--jq", "$ENV.GH_TOKEN|explode"],
        ["pr", "list", "--json", "number", "-q", "env.GH_TOKEN|explode"],
        ["pr", "list", "--json", "number", "-q env.GH_TOKEN | explode"],
        ["api", "repos/{owner}/{repo}", "-q $ENV.GH_TOKEN | explode"],
    ],
)
def test_review_probes_are_refused_before_gh_runs(env, real_gh, args):
    with pytest.raises(Refused, match="environment|separate arguments") as e:
        env.gate.dispatch("gh", {"repo": "me/app", "args": args})
    _assert_no_token(str(e.value))
    assert real_gh == []


@pytest.mark.parametrize(
    "args",
    [
        ["api", "repos/{owner}/{repo}", "--jq", "$ENV|tojson"],
        ["api", "repos/{owner}/{repo}", "--jq", '[$ENV[]]|join(" ")|explode|tojson'],
        ["api", "repos/{owner}/{repo}", "--jq", "$ENV.GH_TOKEN|explode|implode|ascii_downcase"],
        ["api", "repos/{owner}/{repo}", "--jq", "env|tojson|@base64"],
        ["api", "repos/{owner}/{repo}", "--jq", "[env[]|explode]|tojson"],
        ["pr", "list", "--json", "number", "-q", "env|tojson"],
        ["pr", "list", "--json", "number", "-q", "[$ENV[]|ascii_downcase]|tojson"],
    ],
)
def test_gh_environment_holds_no_token_even_if_jq_reads_it(env, real_gh, monkeypatch, args):
    """Second layer: with the jq check switched off, gh's whole environment is still token-free."""
    monkeypatch.setattr(ghpolicy, "_check_jq", lambda name, value: None, raising=False)
    try:
        out = env.gate.dispatch("gh", {"repo": "me/app", "args": args})
    except Refused as e:  # e.g. `explode cannot be applied to: null`: there is no GH_TOKEN to read
        out = str(e)
    _assert_no_token(out)
    # gh did authenticate, from the per-call hosts file: the token reached the API, not the output.
    assert real_gh and all(auth == f"token {TOKEN}" for _, _, auth in real_gh)
    assert not [p for p in env.state.rglob("*") if p.is_file() and TOKEN in p.read_text(errors="replace")]


def test_gh_environment_as_gh_sees_it(env, real_gh, monkeypatch):
    monkeypatch.setattr(ghpolicy, "_check_jq", lambda name, value: None, raising=False)
    out = env.gate.dispatch("gh", {"repo": "me/app", "args": ["api", "repos/{owner}/{repo}", "--jq", "$ENV|tojson"]})
    seen = json.loads(out.splitlines()[-1])
    assert {"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"}.isdisjoint(seen)
    assert not [k for k, v in seen.items() if TOKEN in v or "[token]" in v]
    assert seen["GH_CONFIG_DIR"].startswith(str(env.state / "tmp") + "/")
