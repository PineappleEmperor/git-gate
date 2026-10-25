import io
import json
import stat
import subprocess
import urllib.error
from pathlib import Path

import pytest

from git_gate.gate import Gate

TOKEN = "github_pat_TESTTOKEN0123456789"


def git(*args: str, cwd: Path | None = None, env: dict | None = None, input: str | None = None) -> str:
    base = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(cwd or Path.cwd()),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    out = subprocess.run(
        ["git", "-c", "init.defaultBranch=main", "-c", "protocol.file.allow=always", *args],
        cwd=cwd, env=base | (env or {}), check=True, capture_output=True, text=True, input=input,
    )  # fmt: skip
    return out.stdout.strip()


def commit(repo: Path, path: str, content: str = "x", message: str | None = None) -> str:
    f = repo / path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content)
    git("add", path, cwd=repo)
    git("commit", "-q", "-m", message or f"edit {path}", cwd=repo)
    return git("rev-parse", "HEAD", cwd=repo)


class FakeGitHub:
    """Branch state comes from the bare remote; protection, rules and PRs are set by tests."""

    def __init__(self, remote_root: Path):
        self.remote_root = remote_root
        self.protected: set[str] = set()
        self.rules: dict[str, set[str]] = {}
        self.pulls: dict[str, list[dict]] = {}
        self.open_pulls: dict[str, str] = {}
        self.deleted: list[tuple[str, str]] = []
        self.fail_branch: Exception | None = None

    def __call__(self, token: str) -> FakeGitHub:
        assert token == TOKEN
        return self

    def _bare(self, repo: str) -> Path:
        return self.remote_root / f"{repo}.git"

    def default_branch(self, repo: str) -> str:
        return git("symbolic-ref", "--short", "HEAD", cwd=self._bare(repo))

    def branch(self, repo: str, branch: str):
        if self.fail_branch is not None:
            raise self.fail_branch
        try:
            sha = git("rev-parse", "--verify", f"refs/heads/{branch}", cwd=self._bare(repo))
        except subprocess.CalledProcessError:
            return None
        return {"sha": sha, "protected": branch in self.protected}

    def rule_types(self, repo: str, branch: str) -> set[str]:
        return self.rules.get(branch, set())

    def pulls_from(self, repo: str, branch: str) -> list[dict]:
        return self.pulls.get(branch, [])

    def open_pull(self, repo: str, branch: str) -> str | None:
        return self.open_pulls.get(branch)

    def delete_branch(self, repo: str, branch: str) -> None:
        self.deleted.append((repo, branch))
        git("update-ref", "-d", f"refs/heads/{branch}", cwd=self._bare(repo))


@pytest.fixture
def env(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    cfg = tmp_path / "cfg"
    state = tmp_path / "state"
    remote = tmp_path / "remote"
    for d in (ws, cfg, remote / "me"):
        d.mkdir(parents=True)
    monkeypatch.setenv("GIT_GATE_STATE_DIR", str(state))
    (cfg / "rules.toml").write_text(
        f'workspaces = ["{ws}"]\n'
        'protected_branches = ["main", "release/*"]\n'
        "[repos]\n"
        '"me/app" = "write"\n'
        '"other/lib" = "read"\n'
    )
    token = cfg / "token"
    token.write_text(TOKEN + "\n")
    token.chmod(0o600)

    seed = tmp_path / "seed"
    seed.mkdir()
    git("init", "-q", cwd=seed)
    commit(seed, "README.md", "hello")
    git("clone", "-q", "--bare", str(seed), str(remote / "me/app.git"), cwd=tmp_path)

    api = FakeGitHub(remote)
    gate = Gate(cfg, state, base_url=f"file://{remote}", github=api)

    class Env:
        pass

    e = Env()
    e.ws, e.cfg, e.state, e.remote, e.api, e.gate, e.tmp = ws, cfg, state, remote, api, gate, tmp_path
    e.bare = remote / "me/app.git"
    e.url = f"file://{remote}/me/app.git"
    return e


@pytest.fixture
def checkout(env):
    """An agent checkout of me/app inside the workspace, made by plain git."""
    path = env.ws / "app"
    git("clone", "-q", env.url, str(path), cwd=env.tmp)
    return path


def call(env, tool: str, **kwargs) -> str:
    return env.gate.call(tool, getattr(env.gate, tool), **kwargs)


def make_bundle(checkout: Path, out: Path, branch: str, *exclude: str) -> Path:
    """What an agent runs in the sandbox before calling push."""
    git("bundle", "create", "-q", str(out), branch, "--not", *(exclude or ("--remotes=origin",)), cwd=checkout)
    return out


def push(env, checkout: Path, branch: str, **kwargs) -> str:
    """Bundle `branch` from the agent's checkout into the workspace and push it through the gate."""
    out = env.ws / f"{branch.replace('/', '-')}.bundle"
    out.unlink(missing_ok=True)
    make_bundle(checkout, out, branch)
    return call(env, "push", repo=kwargs.pop("repo", "me/app"), branch=branch, bundle=str(out), **kwargs)


API = "https://api.github.com/"
URL = "https://github.com/me/app/pull/7"


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Stands in for the urllib opener: records each request, answers from canned replies."""

    def __init__(self, replies=None):
        self.replies = replies or {}
        self.calls = []

    def open(self, req, timeout=None):
        path = req.full_url.removeprefix(API)
        body = json.loads(req.data) if req.data else None
        self.calls.append((req.get_method(), path, body))
        reply = self.replies.get((req.get_method(), path), {"html_url": URL})
        if isinstance(reply, int):
            raise urllib.error.HTTPError(req.full_url, reply, "status", {}, None)
        if isinstance(reply, tuple):  # (status, message, headers): an error with a GitHub-style body
            status, message, headers = reply
            fp = io.BytesIO(json.dumps({"message": message}).encode())
            raise urllib.error.HTTPError(req.full_url, status, "status", headers, fp)
        return FakeResponse(json.dumps(reply).encode())


@pytest.fixture
def fake_gh(env):
    """A stand-in gh that records its call; `999` among its arguments makes it fail."""
    record = env.tmp / "gh-call.json"
    script = env.tmp / "gh"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys, pathlib\n"
        f"rec = pathlib.Path({str(record)!r})\n"
        "args = sys.argv[1:]\n"
        "files = {a: pathlib.Path(a).read_text() for a in args if a.startswith('/') and pathlib.Path(a).is_file()}\n"
        "cfg = pathlib.Path(os.environ.get('GH_CONFIG_DIR', '/nonexistent'))\n"
        "hosts = cfg / 'hosts.yml'\n"
        "seen = {'text': hosts.read_text(), 'mode': oct(hosts.stat().st_mode & 0o777),\n"
        "        'dir_mode': oct(cfg.stat().st_mode & 0o777)} if hosts.is_file() else {}\n"
        "rec.write_text(json.dumps({'args': args, 'cwd': os.getcwd(), 'env': dict(os.environ), 'files': files,\n"
        "                           'hosts': seen}))\n"
        "if '999' in args:\n"
        "    print('GraphQL: Could not resolve to a PullRequest with the number of 999.', file=sys.stderr)\n"
        "    sys.exit(1)\n"
        "if '-D' in args:\n"
        "    d = pathlib.Path(args[args.index('-D') + 1]); (d / 'art').mkdir(); (d / 'art/log.txt').write_text('L')\n"
        "    (d / '.claude').mkdir(); (d / '.claude/settings.json').write_text('{}')\n"
        "if '-O' in args:\n"
        "    pathlib.Path(args[args.index('-O') + 1]).write_text('asset')\n"
        "print('ok', os.environ.get('GH_TOKEN') or seen.get('text'))\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    env.gate.gh_bin = str(script)
    return lambda: json.loads(record.read_text())
