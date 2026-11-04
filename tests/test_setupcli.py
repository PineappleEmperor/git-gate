"""gitgatesetup: the plan, the prompt, the apply order, verification and the record's close."""

import copy
import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from git_gate import Refused, setupcli
from git_gate.github import GitHubError
from git_gate.setup import Setups

BIN = Path(__file__).resolve().parents[1] / "bin/gitgatesetup"
TOKEN = "ghu_test"
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
        self.has_branch = branch
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
        return copy.deepcopy(self.live)

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
        return {"sha": "a" * 40, "protected": False} if self.has_branch else None

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


def create(setups, repo=REPO, description=DESCRIPTION, topics=TOPICS, ruleset=RULESET):
    return setups.create(repo, description, topics, ruleset)


class Login:
    def __init__(self, token=TOKEN):
        self.token = token
        self.calls = []

    def __call__(self, client_id, *, say):
        self.calls.append(client_id)
        say("Open https://github.com/login/device and enter ABCD-1234")
        return self.token


def run(argv, gh=None, answer="y\n", login=None):
    out = io.StringIO()
    stdin = answer if hasattr(answer, "readline") else io.StringIO(answer)
    code = setupcli.main(argv, stdin=stdin, stdout=out, login=login or Login(), github=gh or FakeGitHub())
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
        assert TOKEN not in text
    assert "[token]" in out2


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
    assert "repository: absent → create public me/app" in out
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
    proc = subprocess.run([sys.executable, "-m", "git_gate", "setup"], capture_output=True, text=True, check=False)
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
