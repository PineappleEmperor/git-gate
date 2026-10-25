import os

import pytest

from git_gate import Refused
from git_gate.config import load_rules
from git_gate.paths import copy_in, open_exchange, read_workspace_file


@pytest.fixture
def rules(env):
    return load_rules(env.cfg)


def put(env, rel: str, data: bytes = b"body") -> str:
    path = env.ws / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


def _directory(env):
    (env.ws / "notes.md").mkdir()
    return str(env.ws / "notes.md")


def _hard_link(env):
    path = put(env, "notes/body.md")
    os.link(path, env.ws / "notes/other.md")
    return path


def _parent_into_claude(env):
    put(env, ".claude/x.md")
    (env.ws / "docs").symlink_to(env.ws / ".claude")
    return str(env.ws / "docs/x.md")


REFUSED = {
    "directory": _directory,
    "hard-link": _hard_link,
    ".env": lambda env: put(env, ".env"),
    "secrets.yaml": lambda env: put(env, "secrets.yaml"),
    ".storage/x.md": lambda env: put(env, ".storage/x.md"),
    "a/.git/b.md": lambda env: put(env, "a/.git/b.md"),
    "symlinked-parent": _parent_into_claude,
    "final-symlink": lambda env: (env.ws / "l.md").symlink_to(put(env, "real.md")) or str(env.ws / "l.md"),
    "fifo": lambda env: os.mkfifo(env.ws / "f.md") or str(env.ws / "f.md"),
    ".hidden.md": lambda env: put(env, ".hidden.md"),
    "script": lambda env: put(env, "run.sh"),
}


@pytest.mark.parametrize("kind", list(REFUSED))
def test_exchange_reads_refused(env, rules, kind):
    path = REFUSED[kind](env)
    with pytest.raises(Refused):
        read_workspace_file(rules, path, "body")


@pytest.mark.parametrize("rel", [".tmp/body.md", "notes/body.txt", "a/.tmp/b/c.md"])
def test_exchange_reads(env, rules, rel):
    path = put(env, rel, b"hello\n")
    assert read_workspace_file(rules, path, "body") == b"hello\n"


def test_exchange_suffix_follows_the_purpose(env, rules, tmp_path):
    bundle = put(env, ".tmp/x.bundle")
    body = put(env, ".tmp/x.md")
    with pytest.raises(Refused, match=r"\.bundle"):
        copy_in(rules, body, tmp_path / "a", 100, "bundle")
    with pytest.raises(Refused, match=r"\.md"):
        read_workspace_file(rules, bundle, "body")
    copy_in(rules, bundle, tmp_path / "b", 100, "bundle")
    assert (tmp_path / "b").read_bytes() == b"body"


def test_open_exchange_returns_the_real_path(env, rules):
    put(env, "real/x.md")
    (env.ws / "alias").symlink_to(env.ws / "real")
    fd, real = open_exchange(rules, str(env.ws / "alias/x.md"), "body")
    try:
        assert real == env.ws / "real/x.md"
    finally:
        os.close(fd)
