import json
import stat

import pytest

from conftest import TOKEN, call
from git_gate import Refused
from git_gate.ghpolicy import plan

# --- policy --------------------------------------------------------------------------------


def test_repo_is_forced():
    assert plan("me/app", "write", ["pr", "view", "3"]).argv == ["pr", "view", "-R", "me/app", "3"]
    assert plan("me/app", "read", ["repo", "view"]).argv == ["repo", "view", "me/app"]


@pytest.mark.parametrize(
    "args",
    [
        ["pr", "view", "-R", "other/x", "1"],
        ["pr", "view", "--repo=other/x", "1"],
        ["pr", "view", "-Rother/x", "1"],
        ["pr", "view", "https://github.com/other/x/pull/1"],
        ["pr", "view", "github.com/other/x/pull/1"],
        ["pr", "merge", "1", "--admin"],
        ["pr", "merge", "1", "--squash"],
        ["pr", "view", "1", "--web"],
        ["pr", "create", "--recover", "/etc/passwd"],
        ["pr", "list", "--owner", "other"],
        ["pr", "view", "--hostname", "evil.example", "1"],
    ],
)
def test_overrides_refused(args):
    with pytest.raises(Refused):
        plan("me/app", "write", args)


@pytest.mark.parametrize(
    "args,hint",
    [
        (["auth", "token"], "allowlist"),
        (["extension", "install", "x/y"], "allowlist"),
        (["repo", "clone"], "clone tool"),
        (["pr", "checkout", "1"], "fetch"),
        (["release", "create", "v1"], "tags"),
        (["secret", "set", "X"], "allowlist"),
        (["workflow", "run", "ci.yml"], "allowlist"),
        (["issue", "develop", "1"], "remote branch"),
        (["search", "issues", "bug"], "issue list --search"),
        (["search", "prs", "bug"], "pr list --search"),
    ],
)
def test_subcommand_allowlist(args, hint):
    with pytest.raises(Refused, match=hint):
        plan("me/app", "write", args)


@pytest.mark.parametrize(
    "query",
    ["repo:other/x bug", "bug org:other", "-repo:me/app", "user:someone", "is:private", "(owner:x)"],
)
def test_search_qualifiers_refused(query):
    with pytest.raises(Refused, match="--search|separate"):
        plan("me/app", "read", ["issue", "list", "--search", query])
    with pytest.raises(Refused, match="--search|separate"):
        plan("me/app", "read", ["pr", "list", f"--search={query}"])


def test_plain_search_terms_pass():
    plan("me/app", "read", ["pr", "list", "--search", "fix repo layout is:open label:bug"])


def test_write_commands_need_write_access():
    plan("other/lib", "read", ["pr", "list"])
    with pytest.raises(Refused, match="read-only"):
        plan("other/lib", "read", ["pr", "comment", "1", "--body", "hi"])


def test_body_text_with_url_is_fine_but_bare_url_is_not():
    plan("me/app", "write", ["pr", "comment", "1", "--body", "see https://github.com/x/y/pull/2"])
    with pytest.raises(Refused, match="URL"):
        plan("me/app", "write", ["pr", "comment", "1", "--body", "https://github.com/x/y/pull/2"])


def test_body_files_are_marked_for_copy():
    p = plan("me/app", "write", ["pr", "create", "--head", "f", "--title", "t", "--body-file", "/w/b.md"])
    assert p.argv[p.uploads.popitem()[0]] == "/w/b.md"
    p = plan("me/app", "write", ["pr", "comment", "1", "-F", "/w/c.md"])
    ((index, path),) = p.uploads.items()
    assert p.argv[index] == path == "/w/c.md"
    p = plan("me/app", "write", ["pr", "edit", "1", "--body-file=/w/e.md"])
    ((index, path),) = p.uploads.items()
    assert p.argv[index - 1] == "--body-file" and path == "/w/e.md"


def test_download_needs_target():
    with pytest.raises(Refused, match="download"):
        plan("me/app", "read", ["release", "download", "v1"])
    p = plan("me/app", "read", ["run", "download", "5", "-D", "/w/out"])
    assert p.download == (p.argv.index("/w/out"), "/w/out", True)


def test_api_forced_get_inside_repo():
    p = plan("me/app", "read", ["api", "repos/{owner}/{repo}/pulls", "-f", "state=all", "--jq", ".[].number"])
    assert p.argv[:3] == ["api", "--method", "GET"]
    assert p.env == {"GH_REPO": "me/app"}
    plan("home-assistant/core", "read", ["api", "repos/home-assistant/core/contents/x?ref=dev"])
    plan("me/app", "read", ["api", "-X", "GET", "/repos/me/app/releases"])


@pytest.mark.parametrize(
    "args",
    [
        ["api", "-X", "PATCH", "repos/{owner}/{repo}/pulls/1", "-F", "body=@/w/b.md"],
        ["api", "--method=POST", "repos/{owner}/{repo}/issues"],
        ["api", "repos/{owner}/{repo}/contents/x", "-F", "a=@/home/u/.config/git-gate/token"],
        ["api", "repos/{owner}/{repo}/pulls", "--input", "/w/x.json"],
        ["api", "graphql", "-f", "query=mutation{}"],
        ["api", "user"],
        ["api", "repos/other/x/issues"],
        ["api", "repos/{owner}/{repo}/../../other/x"],
        ["api", "repos/me/app/%2e%2e/x"],
        ["api", "https://api.github.com/repos/me/app"],
        ["api", "repos/{owner}/{repo}/pulls", "-H", "X-HTTP-Method-Override: DELETE"],
        ["api", "repos/{owner}/{repo}/pulls", "--unknown-flag"],
        ["api", "repos/{owner}/{repo}", "repos/other/x"],
    ],
)
def test_api_refusals(args):
    with pytest.raises(Refused):
        plan("me/app", "write", args)


# --- runner, with a fake gh ----------------------------------------------------------------


@pytest.fixture
def fake_gh(env):
    record = env.tmp / "gh-call.json"
    script = env.tmp / "gh"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys, pathlib\n"
        f"rec = pathlib.Path({str(record)!r})\n"
        "args = sys.argv[1:]\n"
        "files = {a: pathlib.Path(a).read_text() for a in args if a.startswith('/') and pathlib.Path(a).is_file()}\n"
        "rec.write_text(json.dumps({'args': args, 'cwd': os.getcwd(), 'env': dict(os.environ), 'files': files}))\n"
        "if '-D' in args:\n"
        "    d = pathlib.Path(args[args.index('-D') + 1]); (d / 'art').mkdir(); (d / 'art/log.txt').write_text('L')\n"
        "    (d / '.claude').mkdir(); (d / '.claude/settings.json').write_text('{}')\n"
        "if '-O' in args:\n"
        "    pathlib.Path(args[args.index('-O') + 1]).write_text('asset')\n"
        "print('ok', os.environ.get('GH_TOKEN'))\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    env.gate.gh_bin = str(script)
    return lambda: json.loads(record.read_text())


def test_gh_env_and_scrubbing(env, fake_gh):
    out = call(env, "gh", repo="me/app", args=["pr", "list"])
    assert TOKEN not in out and "[token]" in out
    rec = fake_gh()
    assert rec["args"] == ["pr", "list", "-R", "me/app"]
    assert rec["env"]["GH_TOKEN"] == TOKEN
    assert rec["env"]["GH_CONFIG_DIR"] == str(env.state / "gh")
    assert rec["env"]["GH_PROMPT_DISABLED"] == "1"
    assert rec["cwd"] == str(env.state / "empty")
    assert "GIT_GATE_TOKEN" not in rec["env"]


def test_gh_body_file_is_copied(env, fake_gh):
    body = env.ws / "body.md"
    body.write_text("hello")
    call(env, "gh", repo="me/app", args=["pr", "comment", "1", "--body-file", str(body)])
    rec = fake_gh()
    passed = rec["args"][rec["args"].index("--body-file") + 1]
    assert passed != str(body) and passed.startswith(str(env.state))
    assert rec["files"][passed] == "hello"


def test_gh_body_file_must_be_in_workspace_and_not_symlink(env, fake_gh):
    with pytest.raises(Refused, match="gate-owned"):
        call(env, "gh", repo="me/app", args=["pr", "comment", "1", "-F", str(env.cfg / "rules.toml")])
    with pytest.raises(Refused, match="outside the workspaces"):
        call(env, "gh", repo="me/app", args=["pr", "comment", "1", "-F", str(env.tmp / "seed/README.md")])
    link = env.ws / "sneaky.md"
    link.symlink_to(env.cfg / "token")
    with pytest.raises(Refused):
        call(env, "gh", repo="me/app", args=["pr", "comment", "1", "-F", str(link)])
    with pytest.raises(Refused, match="absolute"):
        call(env, "gh", repo="me/app", args=["pr", "comment", "1", "-F", "body.md"])


def test_gh_refuses_body_containing_token(env, fake_gh):
    body = env.ws / "leak.md"
    body.write_text(f"oops {TOKEN}")
    with pytest.raises(Refused, match="contains the gate's token"):
        call(env, "gh", repo="me/app", args=["pr", "comment", "1", "-F", str(body)])


def test_gh_body_file_must_be_an_exchange_file(env, fake_gh):
    """The gate reads outside the sandbox: a masked secret must never become a comment."""
    record = env.tmp / "gh-call.json"
    for rel in ("secrets.yaml", ".env", ".claude/x.md", "a/.git/x.md"):
        path = env.ws / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("password: hunter2")
        with pytest.raises(Refused):
            call(env, "gh", repo="me/app", args=["pr", "comment", "1", "--body-file", str(path)])
        assert not record.exists()


def test_gh_download_goes_into_a_new_directory(env, fake_gh):
    existing = env.ws / "artifacts"
    existing.mkdir()
    with pytest.raises(Refused, match="already exists"):
        call(env, "gh", repo="me/app", args=["run", "download", "7", "-D", str(existing)])
    assert list(existing.iterdir()) == []
    with pytest.raises(Refused, match="--clobber"):
        plan("me/app", "read", ["run", "download", "7", "-D", "/w/new", "--clobber"])
    for dotted in (".claude", "x/.vscode"):
        with pytest.raises(Refused):
            call(env, "gh", repo="me/app", args=["run", "download", "7", "-D", str(env.ws / dotted)])
    before = sorted(p.name for p in env.ws.iterdir())
    out_dir = env.ws / "new-artifacts"
    out = call(env, "gh", repo="me/app", args=["run", "download", "7", "-D", str(out_dir)])
    rec = fake_gh()
    assert rec["args"][rec["args"].index("-D") + 1].startswith(str(env.state))
    assert (out_dir / "art/log.txt").read_text() == "L"
    assert sorted(p.name for p in env.ws.iterdir()) == sorted([*before, "new-artifacts"])
    assert sorted(str(p.relative_to(out_dir)) for p in out_dir.rglob("*")) == ["art", "art/log.txt"]
    assert "Saved to" in out and "skipped" in out and ".claude" in out


def test_gh_release_download_to_a_new_file(env, fake_gh):
    target = env.ws / "dist/app.tar.gz"
    target.parent.mkdir()
    out = call(env, "gh", repo="me/app", args=["release", "download", "v1", "-O", str(target)])
    assert target.read_text() == "asset" and "Saved to" in out
    with pytest.raises(Refused, match="already exists"):
        call(env, "gh", repo="me/app", args=["release", "download", "v1", "-O", str(target)])
    with pytest.raises(Refused):
        call(env, "gh", repo="me/app", args=["release", "download", "v1", "-O", str(env.ws / ".envrc")])
    assert not (env.ws / ".envrc").exists()


def test_gh_download_into_symlink_refused(env, fake_gh):
    (env.ws / "dl").symlink_to(env.cfg)
    with pytest.raises(Refused):
        call(env, "gh", repo="me/app", args=["run", "download", "7", "-D", str(env.ws / "dl")])
    assert sorted(p.name for p in env.cfg.iterdir()) == ["rules.toml", "token"]
