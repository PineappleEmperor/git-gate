"""gh is retired: no conversation started on this release sees it, and an older one's call refuses."""

import json

import anyio
import pytest

from conftest import call
from git_gate import Refused
from git_gate import call as calls
from git_gate.server import INSTRUCTIONS, build


def test_gh_is_not_registered(env):
    names = {t.name for t in anyio.run(build(env.gate).list_tools)}
    assert "gh" not in names and "gh" not in calls.TOOLS
    assert calls.RETIRED == ("gh",)


def test_instructions_say_agents_never_call_gh():
    assert "never call gh" in INSTRUCTIONS


@pytest.mark.parametrize(
    ("args", "replacement"),
    [
        (["pr", "view", "7"], 'pr tool, action "view"'),
        (["pr", "list", "--state", "all"], 'pr tool, action "list"'),
        (["pr", "diff", "7"], 'pr tool, action "diff"'),
        (["pr", "checks", "7"], "checks tool"),
        (["pr", "checks", "7", "--watch"], "checks tool"),
        (["run", "list"], "runs tool"),
        (["run", "view", "5", "--log-failed"], "run tool"),
        (["run", "watch", "5"], "run tool, with wait=true"),
        (["run", "download", "5"], "not supported"),
        (["issue", "view", "3"], 'issue tool, action "view"'),
        (["issue", "list"], 'issue tool, action "list"'),
        (["release", "list"], 'release tool, action "list"'),
        (["release", "view", "v1"], 'release tool, action "view"'),
        (["release", "download", "v1"], "not supported"),
        (["repo", "view"], 'api_get tool, path ""'),
        (["api", "repos/{owner}/{repo}/labels"], "api_get tool"),
        (["workflow", "list"], 'api_get tool, path "actions/workflows"'),
        (["workflow", "view", "ci.yml"], 'api_get tool, path "actions/workflows"'),
        (["label", "list"], 'api_get tool, path "labels"'),
        (["pr", "create", "--title", "x"], "pr tool, action create"),
        (["run", "rerun", "5"], "workflow_run tool"),
        (["pr", "merge", "7"], "merging"),
        (["browse"], "typed tools"),
        ([], "typed tools"),
    ],
)
def test_a_gh_call_from_an_older_conversation_names_its_replacement(env, args, replacement):
    with pytest.raises(Refused) as e:
        env.gate.dispatch("gh", {"repo": "me/app", "args": args})
    text = str(e.value)
    assert text.startswith("gh is retired")
    assert replacement in text
    assert "new conversation" in text


def test_a_retired_call_is_audited_and_asks_github_nothing(env):
    env.gate.github = None  # any use of GitHub would fail
    with pytest.raises(Refused, match="gh is retired"):
        call(env, "gh", repo="me/app", args=["pr", "view", "7"])
    line = json.loads((env.state / "audit.log").read_text().splitlines()[-1])
    assert (line["tool"], line["outcome"]) == ("gh", "refused")


def test_odd_arguments_still_get_the_refusal(env):
    for args in (None, "pr view", [1, 2], ["pr"]):
        with pytest.raises(Refused, match="gh is retired"):
            env.gate.dispatch("gh", {"repo": "me/app", "args": args})


def test_the_gate_runs_no_gh(env):
    assert not hasattr(env.gate, "gh_bin")
