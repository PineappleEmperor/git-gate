"""gitgatesetup: the plan, the prompt, the apply order, verification and the record's close."""

import copy
import dataclasses
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from git_gate import Refused, setupcli
from git_gate.devicelogin import NOT_CONFIRMED
from git_gate.devicelogin import Login as Tokens
from git_gate.github import GitHubError
from git_gate.handover import EXPIRES, Handovers
from git_gate.setup import Setups

SRC = Path(__file__).resolve().parents[1] / "src"

BIN = Path(__file__).resolve().parents[1] / "bin/gitgatesetup"
TOKEN = "ghu_test"
REFRESH = "ghr_test"
SIGNED_OUT = "Signed out: GitHub revoked this run's token.\n"
SLUG = "git-gate-setup-abc123"
REPO = "me/app"
DESCRIPTION = "An app"
TOPICS = ("home-assistant", "python")
RULESET = {
    "name": "Protect main",
    "target": "branch",
    "enforcement": "active",
    "bypass_actors": [],
    "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
    "rules": [
        {"type": "deletion"},
        {"type": "non_fast_forward"},
        {
            "type": "required_status_checks",
            "parameters": {
                "strict_required_status_checks_policy": False,
                "required_status_checks": [{"context": "ci"}],
            },
        },
    ],
}
WRITES = {"create_user_repo", "edit_repo", "set_topics", "enable_dependency_graph", "create_ruleset", "update_ruleset"}


class FakeGitHub:
    """Stands in for GitHub: keeps a live repository that the writes change, records every call."""

    def __init__(self, *, login="me", repo=None, topics=(), graph=False, branch=True, rulesets=(), checks=()):
        self.login = login
        self.live = repo
        self.live_topics = list(topics)
        self.graph = graph
        self.has_branch = branch  # whether main exists
        self.default_branch = "main"
        self.live_rulesets = [copy.deepcopy(r) for r in rulesets]
        self.checks = list(checks)
        self.fail: dict[str, BaseException] = {}
        self.calls: list[tuple] = []
        self.tokens: list[str] = []

    def __call__(self, token):  # the factory main() is handed
        self.tokens.append(token)
        return self

    def _call(self, name, *args):
        self.calls.append((name, *args))
        if name in self.fail:
            raise self.fail[name]

    def writes(self):
        return [c for c in self.calls if c[0] in WRITES]

    def viewer(self):
        self._call("viewer")
        return self.login

    def repo(self, repo):
        self._call("repo", repo)
        live = copy.deepcopy(self.live)
        if live is not None:
            live["default_branch"] = self.default_branch
        return live

    def create_user_repo(self, name, description):
        self._call("create_user_repo", name, description)
        self.live = {"full_name": f"{self.login}/{name}", "description": description, "has_issues": True,
                     "default_branch": "main"}  # fmt: skip
        self.has_branch = False

    def edit_repo(self, repo, *, description=None, has_issues=None):
        self._call("edit_repo", repo, description, has_issues)
        if description is not None:
            self.live["description"] = description
        if has_issues is not None:
            self.live["has_issues"] = has_issues

    def topics(self, repo):
        self._call("topics", repo)
        return list(self.live_topics)

    def set_topics(self, repo, names):
        self._call("set_topics", repo, tuple(names))
        self.live_topics = list(names)

    def dependency_graph_on(self, repo, live=None):
        self._call("dependency_graph_on", repo)
        return self.graph

    def enable_dependency_graph(self, repo):
        self._call("enable_dependency_graph", repo)
        self.graph = True

    def branch(self, repo, branch):
        self._call("branch", repo, branch)
        return {"sha": "a" * 40, "protected": False} if self.has_branch and branch == "main" else None

    def rulesets(self, repo):
        self._call("rulesets", repo)
        return [{k: r[k] for k in ("id", "name", "target", "source_type")} for r in self.live_rulesets]

    def ruleset(self, repo, id):
        self._call("ruleset", repo, id)
        return copy.deepcopy(next(r for r in self.live_rulesets if r["id"] == id))

    def create_ruleset(self, repo, body):
        self._call("create_ruleset", repo, body)
        self.live_rulesets.append(live_ruleset(body, 50))
        return 50

    def update_ruleset(self, repo, id, body):
        self._call("update_ruleset", repo, id, body)
        self.live_rulesets = [live_ruleset(body, id) if r["id"] == id else r for r in self.live_rulesets]

    def branch_rule_rulesets(self, repo, branch):
        self._call("branch_rule_rulesets", repo, branch)
        own = [{"type": "required_status_checks", "ruleset_source_type": "Repository", "ruleset_source": REPO,
                "ruleset_id": r["id"]} for r in self.live_rulesets]  # fmt: skip
        return own + self.checks


def live_ruleset(body, id, source_type="Repository"):
    """A ruleset as GitHub answers it: the body plus keys GitHub adds."""
    out = copy.deepcopy(body)
    out.update({"id": id, "source_type": source_type, "source": REPO, "node_id": "RRS_x", "_links": {}})
    for rule in out["rules"]:
        if rule["type"] == "required_status_checks":
            for check in rule["parameters"]["required_status_checks"]:
                check["integration_id"] = None
    return out


def live_repo(**kw):
    return {"full_name": REPO, "description": DESCRIPTION, "has_issues": True, "default_branch": "main"} | kw


def all_set(**kw):
    args = {"repo": live_repo(), "topics": TOPICS, "graph": True, "rulesets": [live_ruleset(RULESET, 9)]}
    return FakeGitHub(**(args | kw))


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    state, cfg = tmp_path / "state", tmp_path / "cfg"
    state.mkdir()
    cfg.mkdir()
    monkeypatch.setenv("GIT_GATE_STATE_DIR", str(state))
    monkeypatch.setenv("GIT_GATE_CONFIG_DIR", str(cfg))
    (cfg / "setup-app.json").write_text(json.dumps({"client_id": "Iv1.abc", "slug": SLUG, "owner": "me"}))
    return state, cfg


@pytest.fixture
def setups(dirs):
    return Setups(dirs[0])


def create(setups, repo=REPO, description=DESCRIPTION, topics=TOPICS, ruleset=RULESET, **kw):
    return setups.create(repo, description, topics, ruleset, **kw)


KEPT_SHA = "b" * 40


class FakeKept:
    """Stands in for the kept first push: `push` records itself in the fake GitHub's calls and creates main."""

    def __init__(self, gh, *, open=True, fail=None, pushed=False, lag=False, screen_fail=None):
        self.gh = gh
        self.screen_fail = screen_fail
        self.is_open = open
        self.fail = fail
        self.closed = pushed  # whether the handover was pushed (its record is gone)
        self.lag = lag  # GitHub does not show main yet after the push
        self.screens: list[int] = []
        self.pushes: list[int] = []
        self.opened: list[tuple[int, str]] = []

    def open(self, id, repo):
        self.opened.append((id, repo))
        return KEPT_SHA if self.is_open else None

    def pushed(self, id):
        return self.closed

    def screen(self, id):
        self.screens.append(id)
        if self.screen_fail:
            raise self.screen_fail
        return f"Handover {id} screen\n\nRun: git push {id}"

    def push(self, id, say):
        self.gh.calls.append(("push", id))
        self.pushes.append(id)
        if self.fail:
            raise self.fail
        self.is_open, self.closed = False, True
        self.gh.has_branch = not self.lag
        say(f"Pushed. Handover {id} is closed.")


class Login:
    def __init__(self, token=TOKEN):
        self.token = token
        self.calls = []

    def __call__(self, client_id, *, say, got=None):
        self.calls.append(client_id)
        say("Open https://github.com/login/device and enter ABCD-1234")
        login = Tokens(self.token, REFRESH)
        if got:
            got(login)
        return login


class Revoke:
    def __init__(self, result=True):
        self.result = result
        self.calls = []

    def __call__(self, login, *, alive, say):
        self.calls.append(login)
        say("Signed out: GitHub revoked this run's token." if self.result else "Warning: not confirmed")
        return self.result


def run(argv, gh=None, answer="y\n", login=None, kept=None, revoke=None):
    out = io.StringIO()
    stdin = answer if hasattr(answer, "readline") else io.StringIO(answer)
    gh = gh or FakeGitHub()
    code = setupcli.main(
        argv,
        stdin=stdin,
        stdout=out,
        login=login or Login(),
        github=gh,
        kept=kept or FakeKept(gh),
        revoke=revoke or Revoke(),
    )
    return code, out.getvalue()


def audit_lines(dirs):
    path = dirs[0] / "audit.log"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


# --- the brief's cases ---------------------------------------------------------------------------


def test_fresh_repo_full_run(setups, dirs):
    s = create(setups)
    gh = FakeGitHub(repo=None)
    code, out = run(["1"], gh)
    assert code == 1
    assert gh.tokens == [TOKEN]
    names = [c[0] for c in gh.calls]
    assert names[:2] == ["viewer", "repo"]
    assert gh.writes() == [
        ("create_user_repo", "app", DESCRIPTION),
        ("set_topics", REPO, TOPICS),
        ("enable_dependency_graph", REPO),
    ]
    assert names.index("create_user_repo") < names.index("set_topics") < names.index("enable_dependency_graph")
    assert "Apply? [y/N]" in out
    assert "deferred: main does not exist yet; push it (gitgatepush), then re-run gitgatesetup 1" in out
    assert "Re-run: gitgatesetup 1" in out
    assert setups.get(s.id) == s


def test_ruleset_deferred_until_default_branch_exists(setups):
    create(setups)
    gh = all_set(rulesets=[], branch=False)
    code, out = run(["1"], gh)
    assert code == 1 and gh.writes() == []
    assert "ruleset: deferred: main does not exist yet" in out
    assert "Apply?" not in out  # nothing to apply now
    setups.get(1)

    gh.has_branch = True
    gh.calls.clear()
    code, out = run(["1"], gh)
    assert code == 0, out
    assert gh.writes() == [("create_ruleset", REPO, RULESET)]
    assert "Done. Setup 1 is closed." in out
    with pytest.raises(Refused, match="No open setup 1"):
        setups.get(1)


def test_second_run_changes_nothing_already_applied(setups, dirs):
    create(setups)
    gh = all_set(topics=tuple(reversed(TOPICS)))  # topics compare as sets
    code, out = run(["1"], gh, answer="")
    assert code == 0, out
    assert gh.writes() == []
    for item in ("repository", "description", "Issues", "topics", "dependency graph", "ruleset"):
        assert f"{item}: already set" in out
    assert "Apply?" not in out
    assert "Done. Setup 1 is closed." in out
    assert setups.open() == []


def test_same_name_ruleset_replaced(setups):
    create(setups)
    old = copy.deepcopy(RULESET)
    old["rules"] = old["rules"][:2]
    gh = all_set(rulesets=[live_ruleset({**old, "rules": old["rules"] + [RULESET["rules"][2]]}, 9)])
    gh.live_rulesets[0]["rules"][2]["parameters"]["required_status_checks"] = [{"context": "old"}]
    code, out = run(["1"], gh)
    assert code == 0, out
    assert gh.writes() == [("update_ruleset", REPO, 9, RULESET)]
    assert '"Protect main" (id 9) differs' in out


def test_replacing_a_stricter_ruleset_shows_what_it_removes(setups):
    create(setups)
    strict = copy.deepcopy(RULESET)
    strict["rules"] = [{"type": "deletion"}, {"type": "non_fast_forward"}, {"type": "pull_request", "parameters": {}},
                       {"type": "required_status_checks", "parameters": {
                           "required_status_checks": [{"context": "ci"}, {"context": "li\x1b[31mnt"}]}}]  # fmt: skip
    gh = all_set(rulesets=[live_ruleset(strict, 9)])
    code, out = run(["1"], gh, answer="n\n")
    assert code == 1 and gh.writes() == []
    assert "rules: deletion, non_fast_forward, pull_request, required_status_checks[ci, li?[31mnt] → " in out
    assert "→ deletion, non_fast_forward, required_status_checks[ci]" in out
    assert 'removes: pull_request, check "li?[31mnt"' in out
    assert "\x1b" not in out


def test_replacing_without_dropping_anything_has_no_removes_line(setups):
    create(setups)
    live = copy.deepcopy(RULESET)
    live["enforcement"] = "disabled"
    code, out = run(["1"], all_set(rulesets=[live_ruleset(live, 9)]), answer="n\n")
    assert "differs → replace it. rules: deletion, non_fast_forward, required_status_checks[ci] → " in out
    assert "removes:" not in out


def test_replacing_a_broader_ruleset_shows_the_branches_it_drops(setups):
    create(setups)
    broad = copy.deepcopy(RULESET)
    broad["conditions"]["ref_name"]["include"] = ["~DEFAULT_BRANCH", "refs/heads/rel\x1b[31m/*"]
    gh = all_set(rulesets=[live_ruleset(broad, 9)])
    code, out = run(["1"], gh, answer="n\n")
    assert code == 1 and gh.writes() == []
    assert "branches: include ~DEFAULT_BRANCH, refs/heads/rel?[31m/* → include ~DEFAULT_BRANCH" in out
    assert 'removes: branches "refs/heads/rel?[31m/*"' in out
    assert "\x1b" not in out


def test_replacing_with_a_new_exclude_shows_the_branches_it_drops(setups):
    narrow = copy.deepcopy(RULESET)
    narrow["conditions"]["ref_name"]["exclude"] = ["refs/heads/main"]
    create(setups, ruleset=narrow)
    code, out = run(["1"], all_set(rulesets=[live_ruleset(RULESET, 9)]), answer="n\n")
    assert "branches: include ~DEFAULT_BRANCH → include ~DEFAULT_BRANCH; exclude refs/heads/main" in out
    assert 'removes: branches "refs/heads/main" (newly excluded)' in out


def test_replacing_shows_enforcement_and_bypass_changes(setups):
    create(setups)
    live = copy.deepcopy(RULESET)
    live["enforcement"] = "evaluate"
    live["bypass_actors"] = [{"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}]
    code, out = run(["1"], all_set(rulesets=[live_ruleset(live, 9)]), answer="n\n")
    assert "enforcement: evaluate → active" in out
    assert "bypass actors: 1 → none" in out
    assert "removes:" not in out
    assert "branches:" not in out


def test_an_absent_ruleset_shows_the_wanted_rules(setups):
    create(setups)
    code, out = run(["1"], all_set(rulesets=[]), answer="n\n")
    assert 'ruleset: absent → "Protect main"; rules: deletion, non_fast_forward, required_status_checks[ci]' in out


def test_a_same_name_ruleset_from_elsewhere_is_not_replaced(setups):
    create(setups)
    org = live_ruleset(RULESET, 3, source_type="Organization")
    tag = {**live_ruleset(RULESET, 4), "target": "tag"}
    gh = all_set(rulesets=[org, tag])
    code, out = run(["1"], gh)
    assert code == 0, out
    assert gh.writes() == [("create_ruleset", REPO, RULESET)]
    assert 'ruleset: absent → "Protect main"' in out


def test_decline_applies_nothing(setups, dirs):
    create(setups)
    gh = FakeGitHub(repo=None)
    code, out = run(["1"], gh, answer="n\n")
    assert code == 1 and gh.writes() == []
    assert "Not applied. Setup 1 stays open." in out
    setups.get(1)
    assert audit_lines(dirs)[-1]["outcome"] == "declined"


class Interrupting:
    def readline(self):
        raise KeyboardInterrupt


@pytest.mark.parametrize("answer", ["", Interrupting()], ids=["eof", "ctrl-c"])
def test_eof_or_interrupt_at_the_prompt_declines(setups, answer):
    create(setups)
    gh = FakeGitHub(repo=None)
    code, out = run(["1"], gh, answer=answer)
    assert code == 1 and gh.writes() == []
    assert "Not applied. Setup 1 stays open." in out
    setups.get(1)


def test_interrupt_during_the_repository_create_says_rerun(setups, dirs):
    create(setups)
    gh = FakeGitHub(repo=None)
    gh.fail["create_user_repo"] = KeyboardInterrupt()
    code, out = run(["1"], gh)
    assert code == 1
    assert "\nInterrupted.\nRe-run: gitgatesetup 1\n" in out and "?Interrupted" not in out
    assert audit_lines(dirs)[-1]["outcome"] == "interrupted"
    setups.get(1)


def test_interrupt_during_a_first_item_that_is_not_main_says_rerun(setups, dirs):
    create(setups)
    gh = all_set()
    gh.live["description"] = "old"
    gh.fail["edit_repo"] = KeyboardInterrupt()
    code, out = run(["1"], gh)
    assert code == 1
    assert "\nInterrupted.\nRe-run: gitgatesetup 1\n" in out
    assert audit_lines(dirs)[-1]["outcome"] == "interrupted"
    setups.get(1)


def test_interrupt_before_the_prompt_prints_a_clean_line(setups, dirs):
    create(setups)
    gh = FakeGitHub(repo=None)
    gh.fail["repo"] = KeyboardInterrupt()  # the survey's first read
    code, out = run(["1"], gh)
    assert code == 1 and gh.writes() == []
    assert out.endswith(SIGNED_OUT + "\nInterrupted.\n")
    assert "Re-run" not in out
    assert audit_lines(dirs)[-1]["outcome"] == "refused"


def test_interrupt_after_a_write_says_rerun_and_audits_open(setups, dirs):
    create(setups)
    gh = all_set(topics=(), graph=False)
    gh.live["description"] = "old"
    gh.fail["set_topics"] = KeyboardInterrupt()
    code, out = run(["1"], gh)
    assert code == 1
    assert ("edit_repo", REPO, DESCRIPTION, None) in gh.writes()
    assert "\nInterrupted.\nRe-run: gitgatesetup 1\n" in out
    assert audit_lines(dirs)[-1]["outcome"] == "interrupted"
    setups.get(1)


def test_owner_mismatch_refused(setups, dirs):
    create(setups, repo="SomeOrg/app")
    gh = FakeGitHub(repo=None, login="me")
    code, out = run(["1"], gh)
    assert code == 1 and gh.writes() == []
    assert [c[0] for c in gh.calls] == ["viewer"]
    assert "SomeOrg/app is not under your account (me); gitgatesetup sets up only your own repositories." in out
    setups.get(1)
    assert audit_lines(dirs)[-1]["outcome"] == "refused"


def test_owner_compares_without_case(setups):
    create(setups, repo="Me/app")
    gh = FakeGitHub(repo=None, login="me")
    code, out = run(["1"], gh)
    assert "not under your account" not in out
    assert ("create_user_repo", "app", DESCRIPTION) in gh.writes()


@pytest.mark.parametrize("status", [403, 404])
def test_forbidden_names_the_install_page(setups, dirs, status):
    create(setups)
    gh = all_set(topics=(), graph=False)
    gh.fail["set_topics"] = GitHubError(f"GitHub PUT repos/me/app/topics returned {status}: nope", status, {})
    code, out = run(["1"], gh)
    assert code == 1
    assert f"GitHub refused topics ({status})" in out
    assert f"https://github.com/apps/{SLUG}/installations/new" in out
    assert "not be installed on this repository or may lack a permission" in out
    assert ("enable_dependency_graph", REPO) not in gh.writes()  # it stops at the refusal
    setups.get(1)
    assert audit_lines(dirs)[-1]["outcome"] == "open"


def test_a_private_repo_the_app_cannot_see_points_at_the_install_page(setups, dirs):
    create(setups)
    gh = FakeGitHub(repo=None)
    gh.fail["create_user_repo"] = GitHubError(
        "GitHub POST user/repos returned 422: name already exists on this account", 422, {}
    )
    code, out = run(["1"], gh)
    assert code == 1
    assert "repository: absent (or not visible to the setup app) → create public me/app" in out
    assert "GitHub refused repository (422)" in out
    assert f"https://github.com/apps/{SLUG}/installations/new" in out
    assert "Setup 1 stays open" in out
    setups.get(1)
    assert audit_lines(dirs)[-1]["outcome"] == "open"


def test_another_write_error_keeps_the_setup_open(setups, dirs):
    create(setups)
    gh = all_set(graph=False)
    gh.fail["enable_dependency_graph"] = GitHubError("GitHub PATCH repos/me/app returned 422: invalid", 422, {})
    code, out = run(["1"], gh)
    assert code == 1
    assert "returned 422: invalid" in out and "Setup 1 stays open" in out
    assert "installations/new" not in out
    setups.get(1)


def test_token_never_printed(setups, dirs):
    create(setups)
    gh = FakeGitHub(repo=None)
    code, out = run(["1"], gh)
    gh = all_set(graph=False)
    gh.fail["enable_dependency_graph"] = Refused(f"something echoed {TOKEN} back")
    code, out2 = run(["1"], gh)
    assert code == 1
    for text in (out, out2, (dirs[0] / "audit.log").read_text()):
        assert TOKEN not in text and REFRESH not in text
    assert "[token]" in out2


# --- the first push of main and secret reminders --------------------------------------------------


def test_fresh_repo_with_first_push_closes_in_one_run(setups, dirs):
    create(setups, handover=7)
    gh = FakeGitHub(repo=None)
    kept = FakeKept(gh)
    code, out = run(["1"], gh, kept=kept)
    assert code == 0, out
    shown = out.split("Apply? [y/N]")[0]
    lines = [
        "  repository: absent",
        "  dependency graph: disabled → enabled",
        f"  main: absent → push {KEPT_SHA[:12]} (handover 7; shown below)",
        '  ruleset: absent → "Protect main" after main is pushed; rules: deletion, non_fast_forward, '
        "required_status_checks[ci]",
        "\n\nHandover 7 screen\n\nRun: git push 7\n",
    ]
    at = [shown.index(line) for line in lines]
    assert at == sorted(at)
    assert [c for c in gh.calls if c[0] in WRITES | {"push"}] == [
        ("create_user_repo", "app", DESCRIPTION),
        ("set_topics", REPO, TOPICS),
        ("enable_dependency_graph", REPO),
        ("push", 7),
        ("create_ruleset", REPO, RULESET),
    ]
    assert kept.screens == [7]
    assert out.removesuffix(SIGNED_OUT).endswith("Done. Setup 1 is closed.\n")
    assert setups.open() == []
    assert audit_lines(dirs)[-1]["outcome"] == "closed"


def test_existing_main_is_never_pushed(setups):
    create(setups, handover=7)
    gh = all_set(rulesets=[], branch=True)
    kept = FakeKept(gh)
    code, out = run(["1"], gh, kept=kept)
    assert code == 0, out
    assert "  main: already set" in out
    assert "→ push" not in out
    assert kept.pushes == [] and kept.screens == []
    assert gh.writes() == [("create_ruleset", REPO, RULESET)]


def test_failed_push_keeps_the_setup_open(setups, dirs):
    create(setups, handover=7)
    gh = FakeGitHub(repo=None)
    kept = FakeKept(gh, fail=Refused("git push failed (exit 128). Handover 7 is still open."))
    code, out = run(["1"], gh, kept=kept)
    assert code == 1
    assert "git push failed (exit 128). Handover 7 is still open." in out
    assert "Setup 1 stays open. Re-run: gitgatesetup 1" in out
    assert kept.pushes == [7]
    assert not [c for c in gh.writes() if c[0] in ("create_ruleset", "update_ruleset")]
    setups.get(1)
    assert audit_lines(dirs)[-1]["outcome"] == "open"


def test_default_branch_not_main_defers_the_ruleset(setups):
    create(setups, handover=7)
    gh = FakeGitHub(repo=None)
    gh.default_branch = "master"  # the user's default-branch setting: the new repo reports it once it exists
    kept = FakeKept(gh)
    code, out = run(["1"], gh, kept=kept)
    assert code == 1
    assert kept.pushes == [7]
    assert not [c for c in gh.writes() if c[0] in ("create_ruleset", "update_ruleset")]
    left = out.split("Not done yet:")[1]
    assert "  ruleset: deferred: master does not exist yet; push it (gitgatepush), then re-run gitgatesetup 1" in left
    assert "main:" not in left
    setups.get(1)


def test_gone_handover_falls_back_to_deferred(setups):
    create(setups, handover=7)
    gh = FakeGitHub(repo=None)
    kept = FakeKept(gh, open=False)
    code, out = run(["1"], gh, kept=kept)
    assert code == 1
    assert "ruleset: deferred: main does not exist yet; push it (gitgatepush), then re-run gitgatesetup 1" in out
    assert "  main:" not in out
    assert kept.pushes == [] and kept.screens == []


def test_existing_empty_repo_with_first_push_closes_in_one_run(setups):
    create(setups, handover=7)
    gh = all_set(rulesets=[], branch=False)
    kept = FakeKept(gh)
    code, out = run(["1"], gh, kept=kept)
    assert code == 0, out
    shown = out.split("Apply? [y/N]")[0]
    assert f"  main: absent → push {KEPT_SHA[:12]} (handover 7; shown below)" in shown
    assert '  ruleset: absent → "Protect main" after main is pushed; rules: deletion, non_fast_forward, ' in shown
    assert [c for c in gh.calls if c[0] in WRITES | {"push"}] == [("push", 7), ("create_ruleset", REPO, RULESET)]
    assert out.removesuffix(SIGNED_OUT).endswith("Done. Setup 1 is closed.\n")


def test_existing_empty_repo_with_first_push_shows_the_ruleset_it_replaces(setups):
    create(setups, handover=7)
    strict = copy.deepcopy(RULESET)
    strict["rules"].append({"type": "pull_request", "parameters": {}})
    gh = all_set(rulesets=[live_ruleset(strict, 9)], branch=False)
    kept = FakeKept(gh)
    code, out = run(["1"], gh, kept=kept)
    assert code == 0, out
    shown = out.split("Apply? [y/N]")[0]
    assert '  ruleset: "Protect main" (id 9) differs → replace it after main is pushed. rules: ' in shown
    assert "    removes: pull_request" in shown
    assert "absent →" not in shown.split("  ruleset:")[1]
    assert [c for c in gh.calls if c[0] in WRITES | {"push"}] == [("push", 7), ("update_ruleset", REPO, 9, RULESET)]
    assert out.removesuffix(SIGNED_OUT).endswith("Done. Setup 1 is closed.\n")


def test_known_other_default_branch_does_not_promise_the_ruleset(setups):
    create(setups, handover=7)
    gh = all_set(rulesets=[], branch=False)
    gh.default_branch = "master"
    kept = FakeKept(gh)
    code, out = run(["1"], gh, kept=kept)
    assert code == 1
    deferred = "  ruleset: deferred: master does not exist yet; push it (gitgatepush), then re-run gitgatesetup 1"
    assert deferred in out.split("Apply? [y/N]")[0]
    assert "after main is pushed" not in out
    assert kept.pushes == [7]
    assert not [c for c in gh.writes() if c[0] in ("create_ruleset", "update_ruleset")]
    assert deferred in out.split("Not done yet:")[1]
    setups.get(1)


def test_secrets_are_reminded_at_the_end(setups):
    reminder = [
        "Add these Actions secrets yourself on GitHub (gitgatesetup cannot check them):",
        f"  RELEASE_TOKEN: https://github.com/{REPO}/settings/secrets/actions/new",
    ]
    create(setups, secrets=("RELEASE_TOKEN",))
    code, out = run(["1"], all_set())
    assert code == 0, out
    assert out.splitlines()[-4:-1] == ["Done. Setup 1 is closed.", *reminder]

    create(setups, secrets=("RELEASE_TOKEN",))
    code, out = run(["2"], FakeGitHub(repo=None), answer="n\n")
    assert code == 1
    assert out.splitlines()[-4:-1] == ["Apply? [y/N] Not applied. Setup 2 stays open.", *reminder]

    create(setups)
    code, out = run(["3"], all_set())
    assert "Actions secrets" not in out


def test_default_kept_reads_the_gates_handovers(dirs):
    kept = setupcli._Kept(dirs[0])
    assert kept.open(1, REPO) is None
    h = Handovers(dirs[0]).create(REPO, "main", KEPT_SHA, None, None, "first push", patterns=(), protected=(),
                                  exclude=())  # fmt: skip
    assert kept.open(h.id, REPO) == KEPT_SHA
    assert kept.pushed(h.id) is False
    Handovers(dirs[0]).close(h.id)
    assert kept.pushed(h.id) is True


def first_push_record(dirs, **kw):
    record = {"repo": REPO, "branch": "main", "base": None, "lease": None} | kw
    return Handovers(dirs[0]).create(record["repo"], record["branch"], KEPT_SHA, record["base"], record["lease"],
                                     "first push", patterns=(), protected=(), exclude=())  # fmt: skip


@pytest.mark.parametrize(
    "record",
    [{"repo": "me/other"}, {"branch": "dev"}, {"lease": "c" * 40}, {"base": "d" * 40}],
    ids=["another repo", "another branch", "a lease", "a base"],
)
def test_a_handover_that_is_not_this_setups_first_push_is_not_pushed(setups, dirs, record):
    h = first_push_record(dirs, **record)
    kept = setupcli._Kept(dirs[0])
    reason = f"Handover {h.id} is not a first push of main to {REPO}."
    with pytest.raises(Refused) as e:
        kept.open(h.id, REPO)
    assert str(e.value) == reason
    create(setups, handover=h.id)
    gh = FakeGitHub(repo=None)
    code, out = run(["1"], gh, kept=kept)
    assert code == 1
    assert f"  main: the handover cannot be used: {reason}\n" in out.split("Apply?")[0]
    assert "Pushed." not in out and "→ push" not in out
    assert "ruleset: deferred: main does not exist yet; push it (gitgatepush), then re-run gitgatesetup 1" in out
    Handovers(dirs[0]).get(h.id)  # left for gitgatepush


def test_an_unreadable_handover_record_is_named(setups, dirs):
    create(setups, handover=7)
    handovers = Handovers(dirs[0])
    handovers.dir.mkdir(parents=True, exist_ok=True)
    (handovers.dir / "7.json").write_text("{not json")
    kept = setupcli._Kept(dirs[0])
    with pytest.raises(Refused, match="Handover 7 is unreadable"):
        kept.open(7, REPO)
    gh = FakeGitHub(repo=None)
    code, out = run(["1"], gh, kept=kept)
    assert code == 1
    line = next(x for x in out.splitlines() if x.startswith("  main:"))
    assert line.startswith("  main: the handover cannot be used: Handover 7 is unreadable: ")
    assert line.endswith("is not a handover record.")
    assert "Not done yet:" in out and "  main: the handover cannot be used" in out.split("Not done yet:")[1]
    assert "ruleset: deferred: main does not exist yet; push it (gitgatepush), then re-run gitgatesetup 1" in out
    assert not gh.writes()[3:]  # the repository, topics and graph only: nothing pushed
    assert (handovers.dir / "7.json").exists()


def test_a_screen_that_fails_ends_the_run_before_the_prompt(setups, dirs):
    create(setups, handover=7, secrets=("RELEASE_TOKEN",))
    gh = FakeGitHub(repo=None)
    kept = FakeKept(gh, screen_fail=Refused("Handover 7 changed while waiting; run it again."))
    revoke = Revoke()
    code, out = run(["1"], gh, kept=kept, revoke=revoke)
    assert code == 1
    assert "Apply?" not in out
    assert kept.pushes == [] and not gh.writes()
    assert revoke.calls == [Tokens(TOKEN, REFRESH)]
    assert out.splitlines()[-1] == "Handover 7 changed while waiting; run it again."
    assert "RELEASE_TOKEN" in out
    assert out.index("Signed out") < out.index("Handover 7 changed while waiting")
    assert (audit_lines(dirs)[-1]["outcome"], audit_lines(dirs)[-1]["revoked"]) == ("refused", True)
    setups.get(1)


def test_main_is_read_once_per_survey(setups):
    create(setups, handover=7)
    gh = all_set(rulesets=[], branch=False)
    code, _ = run(["1"], gh, answer="n\n")
    assert code == 1
    assert [c for c in gh.calls if c[0] == "branch"] == [("branch", REPO, "main")]


def test_a_pending_ruleset_survey_still_knows_the_default_branch(setups):
    s = create(setups, handover=7)
    gh = all_set(rulesets=[], branch=False)
    plan = setupcli._survey(gh, s, FakeKept(gh), print)
    assert any(item.name == "ruleset" and "after main is pushed" in item.line for item in plan.items)
    assert plan.default == "main"


def test_default_kept_pushes_only_the_record_it_showed(dirs):
    h = first_push_record(dirs)
    kept = setupcli._Kept(dirs[0])
    assert kept.open(h.id, REPO) == KEPT_SHA
    Handovers(dirs[0])._write(dataclasses.replace(h, sha="c" * 40))
    with pytest.raises(Refused, match=f"Handover {h.id} changed while waiting"):
        kept.push(h.id, print)


def test_default_kept_pushes_nothing_it_did_not_open(dirs):
    h = first_push_record(dirs, repo="me/other")
    kept = setupcli._Kept(dirs[0])
    with pytest.raises(Refused, match="not a first push"):
        kept.open(h.id, REPO)
    with pytest.raises(Refused, match=f"No open handover {h.id}"):
        kept.push(h.id, print)


# --- the rest of the controller's rulings ----------------------------------------------------------


def test_text_from_github_is_cleaned(setups):
    create(setups)
    gh = all_set(repo=live_repo(description="evil\x1b[2J\nline"), topics=("ok\x07",), graph=True)
    code, out = run(["1"], gh, answer="n\n")
    assert "\x1b" not in out and "\x07" not in out
    assert 'description: "evil?[2J?line" → "An app"' in out


def test_new_repo_does_not_resend_description_or_issues(setups):
    create(setups)
    gh = FakeGitHub(repo=None)
    code, out = run(["1"], gh)
    assert not [c for c in gh.writes() if c[0] == "edit_repo"]
    assert "repository: absent (or not visible to the setup app) → create public me/app" in out
    lines = [line.strip().split(":")[0] for line in out.splitlines() if line.startswith("  ")]
    assert lines[:6] == ["repository", "description", "Issues", "topics", "dependency graph", "ruleset"]


def test_existing_repo_edits_description_and_issues(setups):
    create(setups)
    gh = all_set(repo=live_repo(description=None, has_issues=False))
    code, out = run(["1"], gh)
    assert code == 0, out
    assert gh.writes() == [("edit_repo", REPO, DESCRIPTION, None), ("edit_repo", REPO, None, True)]
    assert 'description: (none) → "An app"' in out
    assert "Issues: off → on" in out


def test_a_change_that_does_not_take_keeps_the_setup_open(setups):
    create(setups)
    gh = all_set(graph=False)
    gh.enable_dependency_graph = lambda repo: gh._call("enable_dependency_graph", repo)  # GitHub ignores it
    code, out = run(["1"], gh)
    assert code == 1
    assert "dependency graph: disabled → enabled" in out.split("Apply?")[1]
    assert "Re-run: gitgatesetup 1" in out
    setups.get(1)


def test_an_unreadable_dependency_graph_is_enabled_and_counts_once_accepted(setups):
    create(setups)
    gh = all_set(graph=None)
    gh.enable_dependency_graph = lambda repo: gh._call("enable_dependency_graph", repo)  # still unreadable after
    code, out = run(["1"], gh)
    assert "dependency graph: unknown → enabled" in out
    assert gh.writes() == [("enable_dependency_graph", REPO)]
    assert code == 0, out
    assert "Done. Setup 1 is closed." in out


def test_an_unreadable_dependency_graph_whose_patch_is_refused_stays_open(setups):
    create(setups)
    gh = all_set(graph=None)
    gh.fail["enable_dependency_graph"] = GitHubError("GitHub PATCH repos/me/app returned 403: no", 403, {})
    code, out = run(["1"], gh)
    assert code == 1 and "installations/new" in out
    setups.get(1)


def test_an_enabled_dependency_graph_is_not_patched(setups):
    create(setups)
    gh = all_set(graph=True, topics=())
    code, out = run(["1"], gh)
    assert code == 0, out
    assert "dependency graph: already set" in out
    assert ("enable_dependency_graph", REPO) not in gh.writes()


def test_other_rulesets_requiring_checks_are_reported(setups):
    create(setups)
    checks = [
        {"type": "required_status_checks", "ruleset_source_type": "Repository", "ruleset_source": REPO,
         "ruleset_id": 77},
        {"type": "required_status_checks", "ruleset_source_type": "Organization", "ruleset_source": "acme",
         "ruleset_id": 5},
    ]  # fmt: skip
    gh = all_set(checks=checks)
    code, out = run(["1"], gh)
    assert code == 0, out
    assert "https://github.com/me/app/settings/rules/77" in out
    assert "https://github.com/organizations/acme/settings/rules/5" in out
    assert "settings/rules/9" not in out  # its own ruleset
    assert ("branch_rule_rulesets", REPO, "main") in gh.calls


def test_audit_line_per_run(setups, dirs):
    create(setups)
    run(["1"], all_set())
    (line,) = audit_lines(dirs)
    assert line["tool"] == "gitgatesetup" and line["id"] == 1 and line["repo"] == REPO
    assert line["outcome"] == "closed"
    assert DESCRIPTION not in (dirs[0] / "audit.log").read_text()


def test_deferred_run_audits_open(setups, dirs):
    create(setups)
    run(["1"], FakeGitHub(repo=None))
    assert [line["outcome"] for line in audit_lines(dirs)] == ["open"]


def test_unknown_setup_refuses_before_login(dirs):
    login = Login()
    code, out = run(["7"], login=login)
    assert code == 1 and "No open setup 7." in out
    assert login.calls == []
    (line,) = audit_lines(dirs)
    assert (line["id"], line["repo"], line["outcome"]) == (7, None, "refused")


def test_unregistered_app_refuses_before_login(setups, dirs):
    create(setups)
    (dirs[1] / "setup-app.json").unlink()
    login = Login()
    code, out = run(["1"], login=login)
    assert code == 1 and "run: gitgatesetup --register" in out
    assert login.calls == []


def test_login_uses_the_apps_client_id(setups):
    create(setups)
    login = Login()
    run(["1"], all_set(), login=login)
    assert login.calls == ["Iv1.abc"]


def test_no_argument_lists_open_setups(setups):
    code, out = run([])
    assert code == 0 and out == "no open setups\n"
    create(setups)
    create(setups, repo="me/other", description="Other")
    code, out = run([])
    assert code == 0
    assert out.splitlines() == ["1  me/app  An app", "2  me/other  Other"]


@pytest.mark.parametrize("argv", [["x"], ["1", "2"], ["--register", "1"], ["-1"]])
def test_usage(dirs, argv):
    code, out = run(argv)
    assert code == 2 and out == "usage: gitgatesetup [<id> | --register]\n"


def test_register_runs_the_registration(dirs, monkeypatch):
    seen = []
    monkeypatch.setattr(setupcli, "register", lambda *, say: seen.append(say) or None)
    code, _ = run(["--register"])
    assert code == 0 and len(seen) == 1


def test_register_refusal_exits_1(dirs, monkeypatch):
    def refuse(*, say):
        raise Refused("A setup app is already registered (x)")

    monkeypatch.setattr(setupcli, "register", refuse)
    code, out = run(["--register"])
    assert code == 1 and "already registered" in out


# --- the launcher ----------------------------------------------------------------------------------


def test_the_module_runs_setup(dirs):
    proc = subprocess.run(
        [sys.executable, "-m", "git_gate", "setup"],
        capture_output=True,
        text=True,
        check=False,
        env=os.environ | {"PYTHONPATH": str(SRC)},
    )
    assert proc.returncode == 0 and proc.stdout == "no open setups\n", proc.stderr


def test_the_launcher_runs_only_from_an_installed_release(tmp_path):
    proc = subprocess.run(
        [str(BIN)], env={"PATH": "/usr/bin:/bin", "GIT_GATE_ROOT": str(tmp_path)},
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert proc.returncode == 1 and "refusing to run" in proc.stderr


def test_the_launcher_runs_the_releases_python_isolated(tmp_path):
    release = tmp_path / "releases/abc"
    (release / "bin").mkdir(parents=True)
    shutil.copy(BIN, release / "bin/gitgatesetup")
    python = release / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {tmp_path}/args\n')
    python.chmod(0o755)
    env = {"PATH": "/usr/bin:/bin", "GIT_GATE_ROOT": str(tmp_path)}
    subprocess.run([str(release / "bin/gitgatesetup"), "1"], env=env, check=True)
    assert (tmp_path / "args").read_text().splitlines() == ["-I", "-m", "git_gate", "setup", "1"]


def test_every_run_revokes(setups, dirs):
    cases = [
        (all_set(), "y\n"),  # closing
        (FakeGitHub(repo=None), "n\n"),  # declined
        (FakeGitHub(repo=None, login="other"), "y\n"),  # refused after login: owner mismatch
    ]
    failing = all_set(graph=False)
    failing.fail["enable_dependency_graph"] = Refused("nope")
    cases.append((failing, "y\n"))  # failed write
    for n, (gh, answer) in enumerate(cases, 1):
        create(setups)
        revoke = Revoke()
        _, out = run([str(n)], gh, answer=answer, revoke=revoke)
        assert [(c.token, c.refresh) for c in revoke.calls] == [(TOKEN, REFRESH)]
        assert "Signed out:" in out
    assert [line["revoked"] for line in audit_lines(dirs)] == [True] * 4


def test_revoke_comes_after_the_run_and_the_error_line_last(setups):
    create(setups)
    code, out = run(["1"], FakeGitHub(repo=None, login="other"))
    lines = out.splitlines()
    assert code == 1
    assert lines[-2].startswith("Signed out:") and "not under your account" in lines[-1]


def test_an_unconfirmed_revoke_is_audited_false(setups, dirs):
    create(setups)
    run(["1"], all_set(), revoke=Revoke(result=False))
    assert audit_lines(dirs)[-1]["revoked"] is False


def test_no_login_no_revoke(dirs):
    revoke = Revoke()
    run(["7"], revoke=revoke)
    assert revoke.calls == []
    assert audit_lines(dirs)[-1]["revoked"] is None


def test_alive_is_false_only_for_a_401():
    class Gh:
        def __init__(self, error=None):
            self.error = error

        def viewer(self):
            if self.error:
                raise self.error
            return "me"

    def alive(error):
        return setupcli._alive(lambda token: Gh(error), "t")

    assert alive(None) is True
    assert alive(GitHubError("x", 401, {})) is False
    assert alive(GitHubError("x", 500, {})) is True


def test_alive_counts_a_plain_refusal_as_alive_so_the_loop_goes_on():
    from git_gate.devicelogin import revoke as real_revoke

    answers = [Refused("blip"), Refused("blip"), GitHubError("x", 401, {})]

    class Gh:
        def viewer(self):
            raise answers.pop(0)

    said = []
    ok = real_revoke(
        Tokens("ghu_x", None),
        alive=lambda t: setupcli._alive(lambda token: Gh(), t),
        post=lambda url, body: 202,
        sleep=lambda s: None,
        say=said.append,
    )
    assert ok is True and said == ["Signed out: GitHub revoked this run's token."]


def test_a_revoke_that_raises_still_audits_and_keeps_the_runs_outcome(setups, dirs):
    create(setups)

    def boom(login, *, alive, say):
        raise RuntimeError("bad")

    code, out = run(["1"], all_set(), revoke=boom)
    assert code == 0 and "Could not confirm the revocation (RuntimeError)." in out
    assert audit_lines(dirs)[-1]["outcome"] == "closed" and audit_lines(dirs)[-1]["revoked"] is False


def test_a_revoke_that_raises_does_not_replace_the_runs_error(setups, dirs):
    create(setups)

    def boom(login, *, alive, say):
        raise RuntimeError("bad")

    code, out = run(["1"], FakeGitHub(repo=None, login="other"), revoke=boom)
    assert code == 1 and "not under your account" in out.splitlines()[-1]
    assert audit_lines(dirs)[-1]["outcome"] == "refused"


def test_interrupted_run_revokes_audits_and_ends_on_interrupted(setups, dirs):
    create(setups)
    gh = FakeGitHub(repo=None)
    gh.fail["viewer"] = KeyboardInterrupt()
    revoke = Revoke()
    code, out = run(["1"], gh, revoke=revoke)
    assert code == 1 and len(revoke.calls) == 1
    assert audit_lines(dirs)[-1]["revoked"] is True
    assert out.splitlines()[-1] == "Interrupted."


def test_refresh_token_echoed_in_a_failure_is_redacted(setups):
    create(setups)
    gh = all_set(graph=False)
    gh.fail["enable_dependency_graph"] = Refused(f"echo {REFRESH}")
    code, out = run(["1"], gh)
    assert REFRESH not in out and "[token]" in out


# --- the final fix wave ----------------------------------------------------------------------------


@pytest.mark.parametrize("signum", [signal.SIGHUP, signal.SIGTERM], ids=["SIGHUP", "SIGTERM"])
def test_a_hangup_or_terminate_ends_the_run_as_ctrl_c_does(setups, dirs, signum):
    create(setups)

    class Signalled(FakeGitHub):
        def viewer(self):
            os.kill(os.getpid(), signum)
            return super().viewer()

    caught = []
    previous = signal.signal(signum, lambda n, frame: caught.append(n))  # without the fix, the run goes on
    try:
        before = signal.getsignal(signum)
        revoke = Revoke()
        code, out = run(["1"], Signalled(repo=None), revoke=revoke)
        assert signal.getsignal(signum) is before  # restored on the way out
    finally:
        signal.signal(signum, previous)
    assert caught == []
    assert code == 1 and len(revoke.calls) == 1
    assert out.endswith(SIGNED_OUT + "\nInterrupted.\n")
    assert audit_lines(dirs)[-1]["revoked"] is True
    setups.get(1)


def interrupted_revoke(login, *, alive, say):
    say("")
    raise KeyboardInterrupt


def test_an_interrupt_at_the_end_of_revoke_keeps_the_runs_outcome(setups, dirs):
    create(setups)
    code, out = run(["1"], all_set(), revoke=interrupted_revoke)
    assert code == 0, out
    assert "Interrupted." not in out
    assert out.splitlines()[-4:] == [
        "Done. Setup 1 is closed.",
        "",
        "Could not confirm the revocation (KeyboardInterrupt).",
        NOT_CONFIRMED,
    ]
    assert audit_lines(dirs)[-1]["outcome"] == "closed" and audit_lines(dirs)[-1]["revoked"] is False


def test_an_interrupt_at_the_end_of_revoke_keeps_the_runs_error_line_last(setups, dirs):
    create(setups)
    code, out = run(["1"], FakeGitHub(repo=None, login="other"), revoke=interrupted_revoke)
    assert code == 1
    lines = out.splitlines()
    assert lines[-2] == NOT_CONFIRMED and "not under your account" in lines[-1]
    assert audit_lines(dirs)[-1]["outcome"] == "refused" and audit_lines(dirs)[-1]["revoked"] is False


LAGGING = "  ruleset: deferred: main was pushed; GitHub does not show it yet. Re-run in a moment: gitgatesetup 1"


def test_a_push_github_does_not_show_yet_says_rerun_in_a_moment(setups):
    create(setups, handover=7)
    gh = all_set(rulesets=[], branch=False)
    kept = FakeKept(gh, lag=True)
    code, out = run(["1"], gh, kept=kept)
    assert code == 1 and kept.pushes == [7]
    left = out.split("Not done yet:")[1]
    assert LAGGING in left
    assert "gitgatepush" not in left
    assert not [c for c in gh.writes() if c[0] in ("create_ruleset", "update_ruleset")]
    setups.get(1)


def test_a_rerun_after_a_push_github_does_not_show_yet_says_rerun_in_a_moment(setups):
    create(setups, handover=7)
    gh = all_set(rulesets=[], branch=False)
    code, out = run(["1"], gh, kept=FakeKept(gh, open=False, pushed=True))
    assert code == 1
    assert LAGGING in out and "gitgatepush" not in out
    assert "Apply?" not in out


def test_an_interrupt_during_the_first_push_says_rerun(setups, dirs):
    create(setups, handover=7)
    gh = all_set(rulesets=[], branch=False)
    kept = FakeKept(gh, fail=KeyboardInterrupt())
    code, out = run(["1"], gh, kept=kept)
    assert code == 1 and kept.pushes == [7]
    assert "\nInterrupted.\nRe-run: gitgatesetup 1\n" in out
    assert audit_lines(dirs)[-1]["outcome"] == "interrupted"
    setups.get(1)


# --- the follow-ups --------------------------------------------------------------------------------


class FailsLater(FakeGitHub):
    """Raises `error` on the `nth` call of `name`, once."""

    def __init__(self, name, nth, error, **kw):
        super().__init__(**kw)
        self.when = (name, nth, error)
        self.seen = 0

    def _call(self, name, *args):
        super()._call(name, *args)
        if name == self.when[0]:
            self.seen += 1
            if self.seen == self.when[1]:
                raise self.when[2]


def interrupted_later(name, nth, **kw):
    args = {"repo": live_repo(), "topics": (), "graph": True, "rulesets": [live_ruleset(RULESET, 9)]} | kw
    return FailsLater(name, nth, KeyboardInterrupt(), **args)


@pytest.mark.parametrize(
    ("name", "nth"),
    [("repo", 2), ("topics", 2), ("ruleset", 2), ("branch_rule_rulesets", 1)],
    ids=["read-back of the repository", "read-back of topics", "read-back of the ruleset", "other rulesets"],
)
def test_an_interrupt_after_applying_says_rerun_and_audits_interrupted(setups, dirs, name, nth):
    create(setups, secrets=("RELEASE_TOKEN",))
    gh = interrupted_later(name, nth)
    code, out = run(["1"], gh)
    assert code == 1
    assert ("set_topics", REPO, TOPICS) in gh.writes()
    assert out.splitlines()[-5:] == [
        "Interrupted.",
        "Re-run: gitgatesetup 1",
        "Add these Actions secrets yourself on GitHub (gitgatesetup cannot check them):",
        f"  RELEASE_TOKEN: https://github.com/{REPO}/settings/secrets/actions/new",
        SIGNED_OUT.strip(),
    ]
    assert out.count("Interrupted.") == 1
    assert (audit_lines(dirs)[-1]["outcome"], audit_lines(dirs)[-1]["revoked"]) == ("interrupted", True)
    setups.get(1)


def test_an_interrupt_during_the_secrets_reminder_keeps_the_runs_outcome(setups, dirs):
    create(setups, secrets=("RELEASE_TOKEN",))
    out = io.StringIO()

    class Stops(io.StringIO):
        def write(self, text):
            if "RELEASE_TOKEN:" in text:
                raise KeyboardInterrupt
            return out.write(text)

    code = setupcli.main(["1"], stdin=io.StringIO("y\n"), stdout=Stops(), login=Login(), github=all_set(),
                         kept=FakeKept(all_set()), revoke=Revoke())  # fmt: skip
    assert code == 0, out.getvalue()
    assert "Done. Setup 1 is closed." in out.getvalue()
    assert audit_lines(dirs)[-1]["outcome"] == "closed"


ODD_SHAPES = {
    "null rules": lambda r: r.update(rules=None),
    "string rules": lambda r: r.update(rules="deletion"),
    "rules of non-dicts": lambda r: r.update(rules=[1, "deletion", None]),
    "a dict for rules": lambda r: r.update(rules={"type": "deletion"}),
    "a list for a rule's type": lambda r: r["rules"][0].update(type=["deletion"]),
    "null parameters": lambda r: r["rules"][2].update(parameters=None),
    "string parameters": lambda r: r["rules"][2].update(parameters="ci"),
    "a number for required checks": lambda r: r["rules"][2]["parameters"].update(required_status_checks=5),
    "a dict for required checks": lambda r: r["rules"][2]["parameters"].update(required_status_checks={"a": 1}),
    "required checks of non-dicts": lambda r: r["rules"][2]["parameters"].update(required_status_checks=["ci"]),
    "a list for a check's context": lambda r: r["rules"][2]["parameters"].update(
        required_status_checks=[{"context": ["ci"]}]
    ),
    "null conditions": lambda r: r.update(conditions=None),
    "string conditions": lambda r: r.update(conditions="~DEFAULT_BRANCH"),
    "a list for ref_name": lambda r: r["conditions"].update(ref_name=["~DEFAULT_BRANCH"]),
    "a dict for include": lambda r: r["conditions"]["ref_name"].update(include={"~DEFAULT_BRANCH": 1}),
    "include of non-strings": lambda r: r["conditions"]["ref_name"].update(include=[["~DEFAULT_BRANCH"]]),
    "null bypass actors": lambda r: r.update(bypass_actors=None),
    "string bypass actors": lambda r: r.update(bypass_actors="none"),
    "a dict for bypass actors": lambda r: r.update(bypass_actors={"actor_id": 1}),
    "bypass actors of non-dicts": lambda r: r.update(bypass_actors=[1]),
}


@pytest.mark.parametrize("change", ODD_SHAPES.values(), ids=ODD_SHAPES.keys())
def test_a_live_ruleset_of_an_odd_shape_is_unreadable_and_replaced(setups, change):
    create(setups)
    odd = live_ruleset(RULESET, 9)
    change(odd)
    gh = all_set(rulesets=[odd])
    code, out = run(["1"], gh)
    shown = out.split("Apply?")[0]
    assert (
        '  ruleset: "Protect main" (id 9) differs → replace it. rules: unreadable → deletion, non_fast_forward, '
        "required_status_checks[ci]\n    GitHub's copy has an unexpected shape; it is replaced whole.\n" in shown
    )
    assert gh.writes() == [("update_ruleset", REPO, 9, RULESET)]
    assert code == 0, out


def test_a_live_ruleset_that_is_not_an_object_is_unreadable_and_replaced(setups):
    create(setups)

    class Listed(FakeGitHub):
        def ruleset(self, repo, id):
            got = super().ruleset(repo, id)
            return got if got.get("node_id") == "replaced" else ["not", "a", "ruleset"]

        def update_ruleset(self, repo, id, body):
            super().update_ruleset(repo, id, body)
            self.live_rulesets[0]["node_id"] = "replaced"

    gh = Listed(repo=live_repo(), topics=TOPICS, graph=True, rulesets=[live_ruleset(RULESET, 9)])
    code, out = run(["1"], gh)
    assert "differs → replace it. rules: unreadable → " in out.split("Apply?")[0]
    assert gh.writes() == [("update_ruleset", REPO, 9, RULESET)]
    assert code == 0, out


def test_a_second_run_of_the_same_setup_is_refused_before_sign_in(setups, dirs):
    create(setups)
    login = Login()
    with Setups(dirs[0]).running(1):
        code, out = run(["1"], all_set(), login=login)
    assert code == 1
    assert out == "Setup 1 is already running.\n"
    assert login.calls == []
    assert audit_lines(dirs)[-1]["outcome"] == "refused" and audit_lines(dirs)[-1]["revoked"] is None
    code, out = run(["1"], all_set(), login=login)  # the lock is gone with its holder
    assert code == 0, out


def test_a_run_holds_its_lock_until_it_has_audited(setups, dirs):
    create(setups)
    held = []

    def revoke(login, *, alive, say):
        with pytest.raises(Refused, match="already running"), Setups(dirs[0]).running(1):
            pass
        held.append(True)
        say("Signed out: GitHub revoked this run's token.")
        return True

    code, _ = run(["1"], all_set(topics=()), answer="n\n", revoke=revoke)  # declined: the setup stays open
    assert code == 1 and held == [True]


class DeadAfter(io.StringIO):
    """A terminal that hangs up: writes fail once `dead` is set."""

    def __init__(self, error):
        super().__init__()
        self.error = error
        self.dead = False

    def write(self, text):
        if self.dead:
            raise self.error
        return super().write(text)


@pytest.mark.parametrize(
    "error", [OSError(5, "Input/output error"), BrokenPipeError(32, "Broken pipe"), ValueError("closed file")],
    ids=["EIO", "EPIPE", "closed"],
)  # fmt: skip
def test_a_dead_terminal_after_the_revoke_still_audits_revoked(setups, dirs, error):
    create(setups, secrets=("RELEASE_TOKEN",))
    out = DeadAfter(error)

    def revoke(login, *, alive, say):  # the POST went out and GitHub confirmed; then the terminal hangs up
        out.dead = True
        say("Signed out: GitHub revoked this run's token.")
        return True

    code = setupcli.main(["1"], stdin=io.StringIO("y\n"), stdout=out, login=Login(), github=all_set(),
                         kept=FakeKept(all_set()), revoke=revoke)  # fmt: skip
    assert code == 0
    assert audit_lines(dirs)[-1]["outcome"] == "closed" and audit_lines(dirs)[-1]["revoked"] is True


def test_a_dead_terminal_before_the_run_ends_still_revokes_and_audits(setups, dirs):
    create(setups)
    out = DeadAfter(OSError(5, "Input/output error"))

    class Hangs(FakeGitHub):
        def viewer(self):
            out.dead = True
            return super().viewer()

    revoke = Revoke()
    code = setupcli.main(["1"], stdin=io.StringIO("y\n"), stdout=out, login=Login(), github=Hangs(repo=None),
                         kept=FakeKept(FakeGitHub()), revoke=revoke)  # fmt: skip
    assert code == 1 and len(revoke.calls) == 1
    assert audit_lines(dirs)[-1]["revoked"] is True


@pytest.mark.parametrize("signum", [signal.SIGHUP, signal.SIGTERM], ids=["SIGHUP", "SIGTERM"])
def test_an_ignored_hangup_stays_ignored(setups, signum):
    create(setups)
    during = []

    class Looks(FakeGitHub):
        def viewer(self):
            during.append(signal.getsignal(signum))
            return super().viewer()

    previous = signal.signal(signum, signal.SIG_IGN)  # as nohup leaves SIGHUP
    try:
        code, out = run(["1"], Looks(repo=live_repo(), topics=TOPICS, graph=True, rulesets=[live_ruleset(RULESET, 9)]))
        after = signal.getsignal(signum)
    finally:
        signal.signal(signum, previous)
    assert code == 0, out
    assert during == [signal.SIG_IGN] and after == signal.SIG_IGN


def test_a_hangup_while_the_handlers_go_in_neither_escapes_nor_stays(setups, dirs, monkeypatch):
    create(setups)
    real = signal.signal
    sent = []

    def install(signum, handler):
        old = real(signum, handler)
        if signum == signal.SIGHUP and handler is setupcli._interrupt and not sent:
            sent.append(signum)
            os.kill(os.getpid(), signal.SIGHUP)  # before SIGTERM's handler goes in
        return old

    caught = []
    previous = {n: real(n, lambda n, frame: caught.append(n)) for n in setupcli.HANGUPS}
    monkeypatch.setattr(signal, "signal", install)
    escaped = None
    try:
        before = {n: signal.getsignal(n) for n in setupcli.HANGUPS}
        try:
            code, out = run(["1"], all_set())
        except KeyboardInterrupt as e:
            escaped = e
        after = {n: signal.getsignal(n) for n in setupcli.HANGUPS}
    finally:
        monkeypatch.undo()
        for n, handler in previous.items():
            signal.signal(n, handler)
    assert escaped is None
    assert sent == [signal.SIGHUP] and caught == []
    assert after == before
    assert code == 1 and "Interrupted." in out


def test_a_hangup_while_the_handlers_come_out_neither_escapes_nor_stays(setups, dirs, monkeypatch):
    create(setups)
    real = signal.signal
    sent = []

    def restore(signum, handler):
        old = real(signum, handler)
        if signum == signal.SIGHUP and handler is not setupcli._interrupt and not sent:
            sent.append(signum)
            os.kill(os.getpid(), signal.SIGTERM)  # before SIGTERM's handler comes out
        return old

    caught = []
    previous = {n: real(n, lambda n, frame: caught.append(n)) for n in setupcli.HANGUPS}
    monkeypatch.setattr(signal, "signal", restore)
    escaped = None
    try:
        before = {n: signal.getsignal(n) for n in setupcli.HANGUPS}
        try:
            code, out = run(["1"], all_set())
        except KeyboardInterrupt as e:
            escaped = e
        after = {n: signal.getsignal(n) for n in setupcli.HANGUPS}
    finally:
        monkeypatch.undo()
        for n, handler in previous.items():
            signal.signal(n, handler)
    assert escaped is None
    assert sent == [signal.SIGHUP]
    assert caught == [signal.SIGTERM]  # it reaches the handler the run put back, after the run
    assert after == before
    assert code == 0, out
    assert audit_lines(dirs)[-1]["outcome"] == "closed" and audit_lines(dirs)[-1]["revoked"] is True


def test_an_interrupt_right_after_the_token_is_obtained_still_revokes_it(setups, dirs):
    create(setups)

    class Interrupted(Login):
        def __call__(self, client_id, *, say, got=None):
            self.calls.append(client_id)
            if got:
                got(Tokens(self.token, REFRESH))
            raise KeyboardInterrupt  # in the gap before device_login returns

    revoke = Revoke()
    code, out = run(["1"], all_set(), login=Interrupted(), revoke=revoke)
    assert code == 1
    assert revoke.calls == [Tokens(TOKEN, REFRESH)]
    assert audit_lines(dirs)[-1]["revoked"] is True


def test_tokens_handed_over_mid_sign_in_are_redacted(setups, dirs):
    create(setups)

    class Echoes(Login):
        def __call__(self, client_id, *, say, got=None):
            if got:
                got(Tokens(self.token, REFRESH))
            raise Refused(f"sign-in broke after {TOKEN} and {REFRESH}")

    code, out = run(["1"], all_set(), login=Echoes())
    assert code == 1 and TOKEN not in out and REFRESH not in out and "[token]" in out


EXPIRED = (
    "  ruleset: deferred: main does not exist yet, and handover 7 with its first push has expired; "
    "ask the agent for a new repo_setup"
)


def test_a_handover_purged_on_expiry_is_not_read_as_pushed(setups):
    create(setups, handover=7, created=time.time() - EXPIRES + 60)  # a record from before setups took its time
    gh = all_set(rulesets=[], branch=False)
    code, out = run(["1"], gh, kept=FakeKept(gh, open=False, pushed=True))
    assert code == 1
    assert EXPIRED in out
    assert "was pushed" not in out and "gitgatepush" not in out
    setups.get(1)


def test_an_expired_handover_record_is_not_read_as_pushed_or_pushable(setups, dirs):
    old = time.time() - EXPIRES + 60
    h = Handovers(dirs[0], clock=lambda: old - 120).create(
        REPO, "main", KEPT_SHA, None, None, "first push", patterns=(), protected=(), exclude=()
    )  # its handover is past expiry, its setup is not
    create(setups, handover=h.id, created=old)
    gh = all_set(rulesets=[], branch=False)
    code, out = run(["1"], gh, kept=setupcli._Kept(dirs[0]))
    assert code == 1
    assert EXPIRED.replace("handover 7", f"handover {h.id}") in out
    assert "gitgatepush" not in out


def test_a_handover_pushed_just_now_is_still_read_as_pushed(setups):
    create(setups, handover=7)
    gh = all_set(rulesets=[], branch=False)
    code, out = run(["1"], gh, kept=FakeKept(gh, open=False, pushed=True))
    assert LAGGING in out and "expired" not in out


def test_an_interrupt_once_the_setup_has_closed_audits_closed(setups, dirs):
    create(setups)
    out = io.StringIO()

    class Stops(io.StringIO):
        def write(self, text):
            if text.startswith("Done."):
                raise KeyboardInterrupt
            return out.write(text)

    code = setupcli.main(["1"], stdin=io.StringIO("y\n"), stdout=Stops(), login=Login(), github=all_set(topics=()),
                         kept=FakeKept(FakeGitHub()), revoke=Revoke())  # fmt: skip
    assert code == 0, out.getvalue()
    assert "Re-run" not in out.getvalue()
    assert setups.open() == []
    assert audit_lines(dirs)[-1]["outcome"] == "closed"


# --- fix round 1 -----------------------------------------------------------------------------------


def blocked_now():
    return signal.pthread_sigmask(signal.SIG_BLOCK, [])


def test_a_hangup_just_before_the_restore_blocks_cannot_skip_it(setups, dirs, monkeypatch):
    """The reviewer's probe: SIGHUP lands just before the restore's SIG_BLOCK, while our handler is in."""
    create(setups)
    real = signal.pthread_sigmask
    sent = []

    def probe(how, mask):
        if (
            how == signal.SIG_BLOCK
            and set(mask)
            and signal.getsignal(signal.SIGHUP) is setupcli._interrupt
            and not sent
        ):
            sent.append(how)
            os.kill(os.getpid(), signal.SIGHUP)  # its handler runs before the block below
        return real(how, mask)

    previous = {n: signal.getsignal(n) for n in setupcli.HANGUPS}
    mask = blocked_now()
    monkeypatch.setattr(signal, "pthread_sigmask", probe)
    escaped = None
    try:
        try:
            code, out = run(["1"], all_set())
        except KeyboardInterrupt as e:
            escaped = e
    finally:
        monkeypatch.undo()
        after = {n: signal.getsignal(n) for n in setupcli.HANGUPS}
        left_blocked = blocked_now()
        for n, handler in previous.items():
            signal.signal(n, handler)
        signal.pthread_sigmask(signal.SIG_SETMASK, mask)
    assert sent and escaped is None
    assert after == previous
    assert left_blocked == mask
    assert code == 0, out
    assert "Interrupted." not in out
    assert audit_lines(dirs)[-1]["outcome"] == "closed"


def test_a_pending_ctrl_c_at_the_first_block_leaves_no_mask_and_no_handler(setups, dirs, monkeypatch):
    create(setups)
    real = signal.pthread_sigmask
    raised = []

    def pending(how, mask):
        old = real(how, mask)
        if how == signal.SIG_BLOCK and set(mask) and not raised:
            raised.append(how)
            raise KeyboardInterrupt  # as a Ctrl-C handler run by pthread_sigmask itself
        return old

    previous = {n: signal.getsignal(n) for n in setupcli.HANGUPS}
    mask = blocked_now()
    login = Login()
    monkeypatch.setattr(signal, "pthread_sigmask", pending)
    escaped = None
    try:
        try:
            code, out = run(["1"], all_set(), login=login)
        except KeyboardInterrupt as e:
            escaped = e
    finally:
        monkeypatch.undo()
        after = {n: signal.getsignal(n) for n in setupcli.HANGUPS}
        left_blocked = blocked_now()
        for n, handler in previous.items():
            signal.signal(n, handler)
        signal.pthread_sigmask(signal.SIG_SETMASK, mask)
    assert raised and escaped is None
    assert after == previous and left_blocked == mask
    assert code == 1 and out.endswith("Interrupted.\n") and login.calls == []


def test_an_interrupt_once_the_run_has_returned_keeps_its_code(setups, dirs, monkeypatch):
    create(setups)
    real = setupcli._main

    def then_interrupted(*args):
        real(*args)
        raise KeyboardInterrupt  # lands after the run audited, before main has its code

    monkeypatch.setattr(setupcli, "_main", then_interrupted)
    code, out = run(["1"], all_set())
    assert code == 0, out
    assert "Interrupted." not in out
    assert audit_lines(dirs)[-1]["outcome"] == "closed"


def test_a_refused_read_back_after_applying_says_rerun_and_stays_open(setups, dirs):
    create(setups, secrets=("RELEASE_TOKEN",))
    gh = FailsLater("topics", 2, GitHubError("GitHub GET repos/me/app/topics returned 502: bad gateway", 502, {}),
                    repo=live_repo(), topics=(), graph=True, rulesets=[live_ruleset(RULESET, 9)])  # fmt: skip
    code, out = run(["1"], gh)
    assert code == 1
    assert ("set_topics", REPO, TOPICS) in gh.writes()
    assert out.splitlines()[-5:] == [
        "Apply? [y/N] Reading the changes back failed: GitHub GET repos/me/app/topics returned 502: bad gateway",
        "Setup 1 stays open. Re-run: gitgatesetup 1",
        "Add these Actions secrets yourself on GitHub (gitgatesetup cannot check them):",
        f"  RELEASE_TOKEN: https://github.com/{REPO}/settings/secrets/actions/new",
        SIGNED_OUT.strip(),
    ]
    assert audit_lines(dirs)[-1]["outcome"] == "open"
    setups.get(1)


def test_the_survey_reads_the_setups_clock(setups):
    s = create(setups, handover=7)
    gh = all_set(rulesets=[], branch=False)
    kept = FakeKept(gh, open=False, pushed=True)
    late = setupcli._survey(gh, s, kept, print, clock=lambda: s.created + EXPIRES - 60)
    assert EXPIRED.strip().removeprefix("ruleset: ") in [f"ruleset: {i.line}" for i in late.items][-1]
    early = setupcli._survey(gh, s, kept, print, clock=lambda: s.created + 60)
    assert "was pushed" in early.items[-1].line


def test_the_listing_shows_an_unreadable_record_by_its_id_only(setups, dirs):
    create(setups)
    create(setups, repo="me/other", description="Other")
    path = setups.dir / "2.json"
    data = json.loads(path.read_text())
    data["ruleset"]["bypass_actors"] = [{"actor_id": 1}]
    data["description"] = "evil text"
    path.write_text(json.dumps(data))
    (setups.dir / "3.json").write_text("{not json")
    code, out = run([])
    assert code == 0
    assert out.splitlines() == ["1  me/app  An app", "2: (unreadable)", "3: (unreadable)"]
