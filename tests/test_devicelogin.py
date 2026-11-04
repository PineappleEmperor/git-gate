import webbrowser

import pytest

from git_gate import Refused
from git_gate.devicelogin import device_login

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
    assert h.run() == "ghu_x"
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
    assert h.run(open_url=boom) == "ghu_x"


def test_ctrl_c_is_a_refusal():
    h = Harness([])
    h.sleep = lambda s: (_ for _ in ()).throw(KeyboardInterrupt())
    with pytest.raises(Refused, match="cancelled"):
        h.run()


def test_token_never_said():
    h = Harness([{"access_token": "ghu_x"}])
    h.run()
    assert not any("ghu_x" in line or "dc_secret" in line for line in h.said)
