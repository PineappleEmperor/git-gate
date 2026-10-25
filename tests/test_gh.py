from pathlib import Path

import pytest

from conftest import TOKEN, call
from git_gate import Refused
from git_gate.ghpolicy import plan

# --- policy --------------------------------------------------------------------------------


def test_repo_is_forced():
    assert plan("me/app", ["pr", "view", "3"]).argv == ["pr", "view", "-R", "me/app", "3"]
    assert plan("me/app", ["repo", "view"]).argv == ["repo", "view", "me/app"]


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
        plan("me/app", args)


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
        plan("me/app", args)


@pytest.mark.parametrize(
    "query",
    ["repo:other/x bug", "bug org:other", "-repo:me/app", "user:someone", "is:private", "(owner:x)"],
)
def test_search_qualifiers_refused(query):
    with pytest.raises(Refused, match="--search|separate"):
        plan("me/app", ["issue", "list", "--search", query])
    with pytest.raises(Refused, match="--search|separate"):
        plan("me/app", ["pr", "list", f"--search={query}"])


def test_plain_search_terms_pass():
    plan("me/app", ["pr", "list", "--search", "fix repo layout is:open label:bug"])


@pytest.mark.parametrize(
    "args,tool",
    [
        (["pr", "create", "--head", "f", "--title", "t"], "pr tool"),
        (["pr", "edit", "1", "--title", "t"], "pr tool"),
        (["pr", "edit", "1", "--add-label", "bug"], "triage tool"),
        (["pr", "comment", "1", "--body", "hi"], "pr tool"),
        (["pr", "review", "1", "--approve"], "approving is the user's"),
        (["pr", "review", "1", "--request-changes", "-b", "no"], "pr tool"),
        (["pr", "close", "1"], "pr tool"),
        (["pr", "reopen", "1"], "pr tool"),
        (["pr", "ready", "1"], "pr tool"),
        (["issue", "create", "--title", "t"], "issue tool"),
        (["issue", "edit", "1", "--add-assignee", "me"], "triage tool"),
        (["issue", "comment", "1", "--body", "hi"], "issue tool"),
        (["issue", "close", "1"], "issue tool"),
        (["issue", "reopen", "1"], "issue tool"),
        (["run", "rerun", "5"], "workflow_run"),
        (["run", "rerun", "5", "--failed"], "workflow_run"),
        (["run", "cancel", "5"], "workflow_run"),
        (["label", "create", "bug"], "label tool"),
        (["label", "edit", "bug", "--color", "ff0000"], "label tool"),
    ],
)
def test_gh_writes_point_to_typed_tools(args, tool):
    with pytest.raises(Refused, match=tool):
        plan("me/app", args)


def test_gh_reads_still_pass():
    for args in (
        ["pr", "view", "1", "--comments"],
        ["pr", "list", "--search", "see https://github.com/x/y/pull/2"],
        ["issue", "list", "--label", "bug"],
        ["run", "view", "5", "--log-failed"],
        ["label", "list"],
    ):
        assert plan("me/app", args).argv[:2] == args[:2]


@pytest.mark.parametrize("flag", ["--body-file", "-F", "--body-file=/w/b.md"])
def test_gh_takes_no_body_files(flag):
    """gh is read-only: nothing it runs reads a workspace file."""
    with pytest.raises(Refused, match="typed tools"):
        plan("me/app", ["pr", "list", flag, "/w/b.md"])


def test_gh_write_is_refused_before_gh_runs(env, fake_gh):
    with pytest.raises(Refused, match="pr tool"):
        call(env, "gh", repo="me/app", args=["pr", "comment", "1", "--body", "hi"])
    assert not (env.tmp / "gh-call.json").exists()


def test_download_needs_target():
    with pytest.raises(Refused, match="download"):
        plan("me/app", ["release", "download", "v1"])
    p = plan("me/app", ["run", "download", "5", "-D", "/w/out"])
    assert p.download == (p.argv.index("/w/out"), "/w/out", True)


def test_api_forced_get_inside_repo():
    p = plan("me/app", ["api", "repos/{owner}/{repo}/pulls", "-f", "state=all", "--jq", ".[].number"])
    assert p.argv[:3] == ["api", "--method", "GET"]
    assert p.env == {"GH_REPO": "me/app"}
    plan("home-assistant/core", ["api", "repos/home-assistant/core/contents/x?ref=dev"])
    plan("me/app", ["api", "-X", "GET", "/repos/me/app/releases"])


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
        plan("me/app", args)


# --- runner, with a fake gh ----------------------------------------------------------------


def test_gh_env_and_scrubbing(env, fake_gh):
    out = call(env, "gh", repo="me/app", args=["pr", "list"])
    assert TOKEN not in out and "[token]" in out
    rec = fake_gh()
    assert rec["args"] == ["pr", "list", "-R", "me/app"]
    assert rec["env"]["GH_PROMPT_DISABLED"] == "1"


def test_gh_takes_the_token_from_a_private_per_call_hosts_file(env, fake_gh):
    """The token is never in gh's environment, where a jq filter could read it (C1)."""
    call(env, "gh", repo="me/app", args=["pr", "list"])
    call(env, "gh", repo="me/app", args=["pr", "view", "1"])
    rec = fake_gh()
    assert not [k for k, v in rec["env"].items() if TOKEN in v or k in ("GH_TOKEN", "GITHUB_TOKEN")]
    cfg = Path(rec["env"]["GH_CONFIG_DIR"])
    assert cfg.parent.parent == env.state / "tmp"
    assert rec["hosts"]["mode"] == "0o600" and rec["hosts"]["dir_mode"] == "0o700"
    assert f'oauth_token: "{TOKEN}"' in rec["hosts"]["text"]
    assert rec["hosts"]["text"].startswith("github.com:\n")
    assert not cfg.exists()
    assert not [p for p in env.state.rglob("*") if p.is_file() and TOKEN in p.read_text(errors="replace")]
    assert rec["env"]["GH_PROMPT_DISABLED"] == "1"
    assert rec["cwd"] == str(env.state / "empty")
    assert "GIT_GATE_TOKEN" not in rec["env"]


def test_gh_download_goes_into_a_new_directory(env, fake_gh):
    existing = env.ws / "artifacts"
    existing.mkdir()
    with pytest.raises(Refused, match="already exists"):
        call(env, "gh", repo="me/app", args=["run", "download", "7", "-D", str(existing)])
    assert list(existing.iterdir()) == []
    with pytest.raises(Refused, match="--clobber"):
        plan("me/app", ["run", "download", "7", "-D", "/w/new", "--clobber"])
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
