import json
import stat
import threading
import urllib.error
import urllib.request

import pytest

from git_gate import Refused
from git_gate.appreg import AppConfig, _convert, load_app, manifest, register, serve

BODY = {
    "client_id": "Iv1.pub",
    "slug": "git-gate-setup-abc123",
    "owner": {"login": "someone", "id": 7},
    "client_secret": "SECRET-ONE",
    "pem": "SECRET-PEM",
    "webhook_secret": "SECRET-HOOK",
}


def test_manifest_permissions_exact():
    m = manifest("git-gate setup abc123", "http://127.0.0.1:1/done")
    assert m["default_permissions"] == {"administration": "write", "metadata": "read"}
    assert "hook_attributes" not in m
    assert m["public"] is False and m["default_events"] == [] and m["request_oauth_on_install"] is False
    assert m["redirect_url"] == "http://127.0.0.1:1/done"
    assert m["url"] == "https://github.com/PineappleEmperor/git-gate"


def fake_serve(form_for, state, *, ready=None):
    ready(4321)
    return "c0de"


def run_register(tmp_path, convert=lambda code: BODY):
    said, opened = [], []
    app = register(
        say=said.append,
        open_url=opened.append,
        convert=convert,
        serve=fake_serve,
        cfg=tmp_path,
    )
    return app, said, opened


def test_register_keeps_only_public_fields(tmp_path):
    app, said, opened = run_register(tmp_path)
    assert app == AppConfig("Iv1.pub", "git-gate-setup-abc123", "someone")
    path = tmp_path / "setup-app.json"
    assert json.loads(path.read_text()) == {"client_id": "Iv1.pub", "slug": "git-gate-setup-abc123", "owner": "someone"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    blob = path.read_text() + "\n".join(said)
    for secret in ("SECRET-ONE", "SECRET-PEM", "SECRET-HOOK"):
        assert secret not in blob
    url = "https://github.com/apps/git-gate-setup-abc123/installations/new"
    assert opened == ["http://127.0.0.1:4321/", url]
    text = "\n".join(said)
    assert "Registered git-gate-setup-abc123." in text and url in text and "Enable Device Flow" in text
    assert said[0].startswith("Opening GitHub") and "http://127.0.0.1:4321/" in said[1]


def test_register_creates_config_dir_private(tmp_path):
    cfg = tmp_path / "new" / "git-gate"
    register(say=lambda s: None, open_url=lambda u: None, convert=lambda c: BODY, serve=lambda f, s, **k: "c", cfg=cfg)
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o700


def test_register_refuses_existing(tmp_path):
    run_register(tmp_path)
    with pytest.raises(Refused) as e:
        run_register(tmp_path)
    assert "already registered (git-gate-setup-abc123)" in str(e.value) and "setup-app.json" in str(e.value)


def test_register_incomplete_conversion(tmp_path):
    with pytest.raises(Refused):
        run_register(tmp_path, convert=lambda c: {"client_id": "x"})
    assert not (tmp_path / "setup-app.json").exists()


def test_register_name_and_redirect(tmp_path):
    seen = {}

    def fake_serve(form_for, state, *, ready=None):
        seen["html"] = form_for(4321)
        return "c"

    register(say=lambda s: None, open_url=lambda u: None, convert=lambda c: BODY, serve=fake_serve, cfg=tmp_path)
    assert "127.0.0.1:4321/done" in seen["html"] and "git-gate setup " in seen["html"]


def test_load_app_missing(tmp_path):
    with pytest.raises(Refused) as e:
        load_app(tmp_path)
    assert "gitgatesetup --register" in str(e.value)


def test_load_app_roundtrip_and_bad(tmp_path):
    run_register(tmp_path)
    assert load_app(tmp_path).slug == "git-gate-setup-abc123"
    p = tmp_path / "setup-app.json"
    p.write_text("not json")
    with pytest.raises(Refused):
        load_app(tmp_path)
    p.write_text(json.dumps({"client_id": "x", "slug": "y"}))
    with pytest.raises(Refused):
        load_app(tmp_path)
    p.write_text(json.dumps({"client_id": "x", "slug": "y", "owner": ""}))
    with pytest.raises(Refused):
        load_app(tmp_path)


def start_serve(state="st4te", timeout=30.0):
    box = {}
    ready = threading.Event()

    def on_ready(port):
        box["port"] = port
        ready.set()

    def run():
        try:
            box["code"] = serve(lambda port: f"<form>port {port}</form>", state, ready=on_ready, timeout=timeout)
        except Refused as e:
            box["err"] = str(e)

    t = threading.Thread(target=run)
    t.start()
    assert ready.wait(5)
    return box, t


def get(port, path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_serve_happy_path():
    box, t = start_serve()
    status, body = get(box["port"], "/")
    assert status == 200 and f"port {box['port']}" in body
    assert get(box["port"], "/nope")[0] == 404
    status, body = get(box["port"], "/done?code=abc&state=st4te")
    assert status == 200 and "close this tab" in body
    t.join(5)
    assert box["code"] == "abc"


@pytest.mark.parametrize("query", ["?code=abc&state=wrong", "?code=abc"])
def test_state_mismatch_refused(query):
    box, t = start_serve()
    get(box["port"], "/done" + query)
    t.join(5)
    assert "state" in box["err"] and "code" not in box


def test_serve_times_out():
    box, t = start_serve(timeout=0.3)
    t.join(5)
    assert "err" in box and "Gave up" in box["err"]


def test_convert_posts_empty_to_local_server(monkeypatch):
    import http.server

    seen = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            seen["path"] = self.path
            seen["auth"] = self.headers.get("Authorization")
            seen["len"] = self.headers.get("Content-Length")
            raw = json.dumps(BODY).encode()
            self.send_response(201)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.handle_request).start()
    monkeypatch.setattr(
        "git_gate.appreg.CONVERT_URL", f"http://127.0.0.1:{srv.server_port}/app-manifests/{{code}}/conversions"
    )
    assert _convert("c0de")["slug"] == BODY["slug"]
    assert seen == {"path": "/app-manifests/c0de/conversions", "auth": None, "len": "0"}
    srv.server_close()
