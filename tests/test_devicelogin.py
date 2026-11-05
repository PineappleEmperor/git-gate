import http.server
import json
import threading
import webbrowser
from urllib.parse import parse_qs

import pytest

from git_gate import Refused
from git_gate.devicelogin import NOT_CONFIRMED, REVOKED, Login, _post, _post_json, device_login, revoke

URI = "https://github.com/login/device"
FIRST = {
    "device_code": "dc_secret",
    "user_code": "ABCD-1234",
    "verification_uri": URI,
    "interval": 5,
    "expires_in": 900,
}


class Harness:
    def __init__(self, answers, first=FIRST):
        self.answers = list(answers)
        self.first = first
        self.said, self.opened, self.sleeps, self.posts = [], [], [], []
        self.now = 0.0

    def post(self, url, form):
        self.posts.append((url, form))
        return self.first if url.endswith("/login/device/code") else self.answers.pop(0)

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def run(self, open_url=None):
        return device_login(
            "Iv1.abc",
            say=self.said.append,
            open_url=open_url or self.opened.append,
            post=self.post,
            sleep=self.sleep,
            clock=lambda: self.now,
        )


def test_happy_path():
    h = Harness([{"error": "authorization_pending"}, {"access_token": "ghu_x", "token_type": "bearer"}])
    assert h.run() == Login("ghu_x", None)
    text = "\n".join(h.said)
    assert "ABCD-1234" in text and URI in text
    assert h.opened == [URI]
    assert h.sleeps == [5, 5]
    assert "client_id" in h.posts[0][1] and "scope" not in h.posts[0][1]
    assert h.posts[1][1]["grant_type"] == "urn:ietf:params:oauth:grant-type:device_code"


def test_slow_down_adds_five_seconds():
    h = Harness([{"error": "slow_down"}, {"error": "authorization_pending"}, {"access_token": "ghu_x"}])
    h.run()
    assert h.sleeps == [5, 10, 10]


def test_slow_down_uses_larger_interval_from_github():
    h = Harness([{"error": "slow_down", "interval": 30}, {"access_token": "ghu_x"}])
    h.run()
    assert h.sleeps == [5, 30]


def test_denied_and_expired_end_cleanly():
    with pytest.raises(Refused, match="declined") as e:
        Harness([{"error": "access_denied"}]).run()
    assert "dc_secret" not in str(e.value)
    with pytest.raises(Refused, match="expired"):
        Harness([{"error": "expired_token"}]).run()
    h = Harness([{"error": "authorization_pending"}] * 400, first={**FIRST, "expires_in": 12})
    with pytest.raises(Refused, match="expired") as e:
        h.run()
    assert "dc_secret" not in str(e.value) and "ghu_" not in str(e.value)


def test_other_error_names_code_not_device_code():
    with pytest.raises(Refused, match="incorrect_client_credentials") as e:
        Harness([{"error": "incorrect_client_credentials"}]).run()
    assert "dc_secret" not in str(e.value)


def test_incomplete_first_response_refused():
    with pytest.raises(Refused):
        Harness([], first={"user_code": "A", "verification_uri": URI}).run()
    with pytest.raises(Refused, match="not_found"):
        Harness([], first={"error": "not_found"}).run()


def test_open_url_failure_is_not_fatal():
    def boom(url):
        raise webbrowser.Error("no browser")

    h = Harness([{"access_token": "ghu_x"}])
    assert h.run(open_url=boom).token == "ghu_x"


def test_ctrl_c_is_a_refusal():
    h = Harness([])
    h.sleep = lambda s: (_ for _ in ()).throw(KeyboardInterrupt())
    with pytest.raises(Refused, match="cancelled"):
        h.run()


def test_token_never_said():
    h = Harness([{"access_token": "ghu_x"}])
    h.run()
    assert not any("ghu_x" in line or "dc_secret" in line for line in h.said)


# The default post, against local servers on 127.0.0.1 only.


@pytest.fixture
def server():
    """Start servers whose handler is `respond(handler) -> None`; record every request."""
    started = []

    def start(respond):
        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                seen.append((self.command, self.path, dict(self.headers), body, self.server.server_port))
                respond(self)

            def log_message(self, *args):
                pass

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        started.append(srv)
        return f"http://127.0.0.1:{srv.server_port}", seen

    yield start
    for srv in started:
        srv.shutdown()
        srv.server_close()


def _reply(status, body, headers=()):
    def respond(h):
        h.send_response(status)
        for k, v in headers:
            h.send_header(k, v)
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    return respond


def test_post_sends_form_with_json_accept(server):
    base, seen = server(_reply(200, b'{"ok": 1}'))
    assert _post(base + "/login/device/code", {"client_id": "Iv1.abc", "a": "b c"}) == {"ok": 1}
    method, path, headers, body, _ = seen[0]
    assert method == "POST" and path == "/login/device/code"
    assert headers["Accept"] == "application/json"
    assert headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert parse_qs(body.decode()) == {"client_id": ["Iv1.abc"], "a": ["b c"]}


def test_post_refuses_cross_host_redirect(server):
    other, target_seen = server(_reply(200, b"{}"))
    base, seen = server(_reply(307, b"", [("Location", other + "/x")]))
    with pytest.raises(Refused, match="another host"):
        _post(base + "/start", {"client_id": "c"})
    assert len(seen) == 1 and target_seen == []


def test_post_http_error_is_refused_with_capped_snippet(server):
    body = b"x" * 120 + b"access_token=ghu_secret"
    base, _ = server(_reply(500, body))
    with pytest.raises(Refused, match="HTTP 500") as e:
        _post(base + "/t", {})
    assert "ghu_secret" not in str(e.value) and "x" * 120 in str(e.value)


def test_post_non_json_is_refused(server):
    base, _ = server(_reply(200, b"<html>nope</html>"))
    with pytest.raises(Refused, match="JSON"):
        _post(base + "/t", {})


def test_post_json_list_is_refused(server):
    base, _ = server(_reply(200, json.dumps([1, 2]).encode()))
    with pytest.raises(Refused, match="unexpected"):
        _post(base + "/t", {})


def test_login_keeps_the_refresh_token():
    h = Harness([{"access_token": "ghu_x", "refresh_token": "ghr_x"}])
    login = h.run()
    assert login == Login("ghu_x", "ghr_x")
    assert "ghu_x" not in repr(login) and "ghr_x" not in repr(login)


class Revoking:
    def __init__(self, alive=(False,), status=202):
        self.alive_answers = list(alive)
        self.status = status
        self.said, self.posts, self.checks, self.sleeps = [], [], [], []

    def post(self, url, body):
        self.posts.append((url, body))
        return self.status

    def alive(self, token):
        self.checks.append(token)
        return self.alive_answers.pop(0) if len(self.alive_answers) > 1 else self.alive_answers[0]

    def sleep(self, seconds):
        self.sleeps.append(seconds)

    def run(self, sleep=None):
        return revoke(
            Login("ghu_x", "ghr_x"), alive=self.alive, post=self.post, sleep=sleep or self.sleep, say=self.said.append
        )


def test_revoke_posts_both_without_auth(server):
    r = Revoking()
    assert r.run() is True
    assert r.posts == [("https://api.github.com/credentials/revoke", {"credentials": ["ghu_x", "ghr_x"]})]
    assert r.checks == ["ghu_x"] and r.sleeps == [5]
    assert r.said == [REVOKED]
    base, seen = server(_reply(202, b""))
    assert _post_json(base + "/credentials/revoke", {"credentials": ["a"]}) == 202
    _, path, headers, body, _ = seen[0]
    assert path == "/credentials/revoke" and json.loads(body) == {"credentials": ["a"]}
    assert "Authorization" not in headers
    assert headers["Content-Type"] == "application/json" and headers["Accept"] == "application/vnd.github+json"


def test_post_json_reports_an_error_status(server):
    base, _ = server(_reply(422, b'{"message": "x"}'))
    assert _post_json(base + "/t", {}) == 422


def test_revoke_without_refresh_sends_only_the_token():
    r = Revoking()
    revoke(Login("ghu_x", None), alive=r.alive, post=r.post, sleep=r.sleep, say=r.said.append)
    assert r.posts[0][1] == {"credentials": ["ghu_x"]}


def test_revoke_still_alive_warns():
    r = Revoking(alive=(True,))
    assert r.run() is False
    assert len(r.checks) == 6 and r.sleeps == [5] * 6
    assert r.said == [NOT_CONFIRMED]
    assert NOT_CONFIRMED.startswith("Warning: GitHub has not confirmed this run's token is revoked. Revoke it at ")


def test_revoke_refused_warns_with_cause():
    r = Revoking(status=422)
    assert r.run() is False
    assert r.said == ["GitHub refused the revocation (422).", NOT_CONFIRMED]
    assert r.checks == []


def test_revoke_interrupted_warns():
    def interrupt(seconds):
        raise KeyboardInterrupt

    r = Revoking()
    assert r.run(sleep=interrupt) is False
    assert r.said[-1] == NOT_CONFIRMED
