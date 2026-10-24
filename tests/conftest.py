import subprocess
from pathlib import Path

import pytest

from git_gate.gate import Gate

TOKEN = "github_pat_TESTTOKEN0123456789"


def git(*args: str, cwd: Path | None = None, env: dict | None = None) -> str:
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
        cwd=cwd, env=base | (env or {}), check=True, capture_output=True, text=True,
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
        self.deleted: list[tuple[str, str]] = []

    def __call__(self, token: str) -> FakeGitHub:
        assert token == TOKEN
        return self

    def _bare(self, repo: str) -> Path:
        return self.remote_root / f"{repo}.git"

    def default_branch(self, repo: str) -> str:
        return git("symbolic-ref", "--short", "HEAD", cwd=self._bare(repo))

    def branch(self, repo: str, branch: str):
        try:
            sha = git("rev-parse", "--verify", f"refs/heads/{branch}", cwd=self._bare(repo))
        except subprocess.CalledProcessError:
            return None
        return {"sha": sha, "protected": branch in self.protected}

    def rule_types(self, repo: str, branch: str) -> set[str]:
        return self.rules.get(branch, set())

    def pulls_from(self, repo: str, branch: str) -> list[dict]:
        return self.pulls.get(branch, [])

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
