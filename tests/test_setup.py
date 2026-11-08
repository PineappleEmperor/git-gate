import copy
import json
import stat

import pytest

from conftest import call, commit, git, make_bundle
from git_gate import Refused
from git_gate.gate import InternalError
from git_gate.handover import EXPIRES, Handovers
from git_gate.setup import Setups, check_description, check_ruleset, check_secrets, check_topics

TEMPLATE = json.loads("""
{
  "name": "Protect main",
  "target": "branch",
  "enforcement": "active",
  "conditions": {
    "ref_name": {
      "include": [
        "~DEFAULT_BRANCH"
      ],
      "exclude": []
    }
  },
  "rules": [
    {
      "type": "deletion"
    },
    {
      "type": "non_fast_forward"
    },
    {
      "type": "required_status_checks",
      "parameters": {
        "strict_required_status_checks_policy": true,
        "do_not_enforce_on_create": false,
        "required_status_checks": [
          {
            "context": "pr / CC labelling"
          },
          {
            "context": "pr / CC label validation"
          },
          {
            "context": "lint / CC title validation"
          },
          {
            "context": "Hassfest manifest validation"
          },
          {
            "context": "HACS validation"
          },
          {
            "context": "validate / Python validation"
          },
          {
            "context": "audit / ha-integration conformance check"
          },
          {
            "context": "Dependency review"
          }
        ]
      }
    }
  ],
  "bypass_actors": []
}
""")


def mutated(change) -> bytes:
    data = copy.deepcopy(TEMPLATE)
    change(data)
    return json.dumps(data).encode()


def drop_checks(d):
    d["rules"] = [r for r in d["rules"] if r["type"] != "required_status_checks"]


def empty_checks(d):
    d["rules"][2]["parameters"]["required_status_checks"] = []


def drop_type(kind):
    def change(d):
        d["rules"] = [r for r in d["rules"] if r["type"] != kind]

    return change


REFUSALS = [
    (lambda d: d.update(bypass_actors=[{"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}]),
     "bypass_actors"),
    (lambda d: d.update(enforcement="evaluate"), "enforcement"),
    (lambda d: d.update(target="tag"), "target"),
    (lambda d: d["conditions"]["ref_name"].update(include=["refs/heads/dev"]), "~DEFAULT_BRANCH"),
    (drop_checks, "required status check"),
    (empty_checks, "required status check"),
    (drop_type("deletion"), "deletion"),
    (drop_type("non_fast_forward"), "non_fast_forward"),
    (lambda d: d.update(source="x"), "source"),
    (lambda d: d.update(name=""), "name"),
    (lambda d: d.update(name="x" * 101), "name"),
]  # fmt: skip


@pytest.mark.parametrize(("change", "fragment"), REFUSALS)
def test_ruleset_refusals(change, fragment):
    with pytest.raises(Refused, match=fragment):
        check_ruleset(mutated(change), "/w/r.json")


@pytest.mark.parametrize("data", [b"not json", b"[]", b"\xff", b"[" * 60000])
def test_ruleset_not_an_object(data):
    with pytest.raises(Refused, match="JSON object"):
        check_ruleset(data, "/w/r.json")


def test_ruleset_size():
    pad = b" " * (65_536 - len(json.dumps(TEMPLATE)))
    assert check_ruleset(json.dumps(TEMPLATE).encode() + pad, "/w/r.json") == TEMPLATE
    with pytest.raises(Refused, match="64 KiB"):
        check_ruleset(json.dumps(TEMPLATE).encode() + pad + b" ", "/w/r.json")


def test_ruleset_accepts_template():
    assert check_ruleset(json.dumps(TEMPLATE).encode(), "/w/r.json") == TEMPLATE


def test_topics():
    assert check_topics(["home-assistant", "hacs"]) == ("home-assistant", "hacs")
    for bad in ([], [f"t{i}" for i in range(21)], ["Has-Upper"], ["-lead"], ["a" * 51], ["a", "a"], "abc", [1]):
        with pytest.raises(Refused):
            check_topics(bad)


def test_description():
    assert check_description("x" * 350) == "x" * 350
    assert check_description("  A thing  ") == "A thing"
    for bad in ("x" * 351, "a\nb", "", "   ", 5):
        with pytest.raises(Refused):
            check_description(bad)


def test_store_roundtrip_and_expiry(tmp_path):
    now = [1_000_000.0]
    setups = Setups(tmp_path, clock=lambda: now[0])
    first = setups.create("me/app", "A thing", ("a", "b"), TEMPLATE)
    assert setups.get(first.id) == first
    assert first.topics == ("a", "b")
    assert setups.open() == [first]
    assert stat.S_IMODE((tmp_path / "setups").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "setups" / f"{first.id}.json").stat().st_mode) == 0o600
    setups.close(first.id)
    with pytest.raises(Refused, match=f"No open setup {first.id}."):
        setups.get(first.id)
    second = setups.create("me/app", "A thing", ("a",), TEMPLATE)
    assert second.id > first.id
    now[0] += EXPIRES + 1
    with pytest.raises(Refused, match=f"No open setup {second.id}."):
        setups.get(second.id)
    assert setups.open() == []


def test_ids_stay_distinct_without_the_counter(tmp_path):
    setups = Setups(tmp_path)
    first = setups.create("me/app", "A thing", ("a",), TEMPLATE)
    (tmp_path / "setups" / "next").unlink()
    second = setups.create("me/app", "A thing", ("a",), TEMPLATE)
    assert second.id != first.id


# --- the repo_setup tool -----------------------------------------------------------------------


def _request(env, **over):
    ruleset = env.ws / "ruleset.json"
    ruleset.write_text(json.dumps(TEMPLATE))
    args = dict(repo="me/app", description="A thing", topics=["home-assistant", "hacs"], ruleset_file=str(ruleset))
    return args | over


def test_repo_setup_records_and_answers(env):
    def no_github(token):
        raise AssertionError("repo_setup must not call GitHub")

    env.gate.github = no_github
    out = env.gate.dispatch("repo_setup", _request(env))
    assert out == (
        "Setup 1 recorded for me/app. Ask the user to run, in their own terminal:\n\ngitgatesetup 1\n\n"
        "It signs them in to GitHub in the browser, shows what will change, and applies it. Do not retry."
    )
    s = Setups(env.gate.state).get(1)
    assert s.topics == ("home-assistant", "hacs")
    assert s.repo == "me/app"
    assert s.ruleset == TEMPLATE


def test_repo_setup_needs_write(env):
    with pytest.raises(Refused, match="read-only"):
        call(env, "repo_setup", **_request(env, repo="other/lib"))
    with pytest.raises(Refused, match="allowlist"):
        call(env, "repo_setup", **_request(env, repo="nobody/x"))
    with pytest.raises(Refused, match="owner/name"):
        call(env, "repo_setup", **_request(env, repo="nope"))


def test_repo_setup_ruleset_outside_workspace(env):
    with pytest.raises(Refused):
        call(env, "repo_setup", **_request(env, ruleset_file="/etc/passwd.json"))
    assert not (env.gate.state / "setups").exists() or not list((env.gate.state / "setups").glob("*.json"))


def test_repo_setup_checks_arguments(env):
    with pytest.raises(Refused):
        call(env, "repo_setup", **_request(env, topics=["Bad Topic"]))
    with pytest.raises(Refused):
        call(env, "repo_setup", **_request(env, description="two\nlines"))


def test_check_secrets():
    assert check_secrets(None) == ()
    assert check_secrets(["release_token"]) == ("RELEASE_TOKEN",)
    for bad, fragment in [
        ([], "1 to 10"),
        ([f"S{i}" for i in range(11)], "1 to 10"),
        (["GITHUB_X"], "GITHUB_"),
        (["github_x"], "GITHUB_"),
        (["9X"], "letters, digits and underscores"),
        (["A", "a"], "repeat"),
    ]:
        with pytest.raises(Refused, match=fragment):
            check_secrets(bad)


def test_old_records_still_parse(tmp_path):
    setups = Setups(tmp_path)
    s = setups.create("me/app", "A thing", ("a",), TEMPLATE)
    path = setups.dir / f"{s.id}.json"
    data = json.loads(path.read_text())
    del data["handover"], data["secrets"]
    path.write_text(json.dumps(data))
    old = setups.get(s.id)
    assert old.handover is None
    assert old.secrets == ()


# --- the first push --------------------------------------------------------------------------


def _first_push(env, *exclude, branch="main"):
    """A fresh repo in the workspace whose branch is bundled; returns the bundle path and the commits."""
    src = env.ws / "fresh"
    src.mkdir(exist_ok=True)
    git("init", "-q", cwd=src)
    if branch != "main":
        git("switch", "-q", "-c", branch, cwd=src)
    first = commit(src, "README.md", "hello")
    second = commit(src, ".github/workflows/ci.yml", "on: push")
    out = env.ws / "first.bundle"
    make_bundle(src, out, branch, *[{"first": first}.get(x, x) for x in exclude])
    return out, first, second


def _no_github(env):
    def no_github(token):
        raise AssertionError("repo_setup must not call GitHub")

    env.gate.github = no_github


def test_repo_setup_keeps_the_first_push(env):
    _no_github(env)
    bundle, _, tip = _first_push(env)
    out = env.gate.dispatch("repo_setup", _request(env, bundle=str(bundle)))
    assert "It includes the first push of main (handover 1)." in out
    assert Setups(env.gate.state).get(1).handover == 1
    h = Handovers(env.gate.state).get(1)
    assert (h.branch, h.base, h.sha) == ("main", None, tip)
    assert h.protected == (".github/workflows/ci.yml",)
    ref = git("--git-dir", str(env.gate.mirror.path("me/app")), "rev-parse", "refs/gate/handover/1", cwd=env.tmp)
    assert ref == tip


def test_repo_setup_bundle_needs_complete_history(env):
    bundle, _, _ = _first_push(env, "first")
    with pytest.raises(Refused, match="complete history"):
        call(env, "repo_setup", **_request(env, bundle=str(bundle)))
    assert not list((env.gate.state / "setups").glob("*.json"))
    assert Handovers(env.gate.state).open() == []


def test_repo_setup_bundle_needs_complete_history_even_when_the_mirror_holds_the_prerequisites(env):
    bundle, first, _ = _first_push(env)
    call(env, "repo_setup", **_request(env, bundle=str(bundle)))  # the mirror now holds both commits
    partial = make_bundle(env.ws / "fresh", env.ws / "partial.bundle", "main", first)
    with pytest.raises(Refused, match="complete history"):
        call(env, "repo_setup", **_request(env, bundle=str(partial)))
    assert len(list((env.gate.state / "setups").glob("*.json"))) == 1
    assert [h.id for h in Handovers(env.gate.state).open()] == [1]


def test_repo_setup_closes_its_handover_when_the_request_cannot_be_recorded(env, monkeypatch):
    bundle, _, _ = _first_push(env)

    def broken(self, *args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Setups, "create", broken)
    with pytest.raises(InternalError, match="disk full"):
        call(env, "repo_setup", **_request(env, bundle=str(bundle)))
    assert Handovers(env.gate.state).open() == []
    refs = git("--git-dir", str(env.gate.mirror.path("me/app")), "for-each-ref", "refs/gate/handover", cwd=env.tmp)
    assert refs == ""


def test_repo_setup_bundle_must_hold_main(env):
    bundle, _, _ = _first_push(env, branch="feat")
    with pytest.raises(Refused, match="refs/heads/main"):
        call(env, "repo_setup", **_request(env, bundle=str(bundle)))
    assert not list((env.gate.state / "setups").glob("*.json"))
    assert Handovers(env.gate.state).open() == []


def test_repo_setup_records_secrets(env):
    out = env.gate.dispatch("repo_setup", _request(env, secrets=["release_token"]))
    assert Setups(env.gate.state).get(1).secrets == ("RELEASE_TOKEN",)
    assert "It reminds the user to add the Actions secrets: RELEASE_TOKEN." in out


# --- the record is checked again on load, and a run holds its lock -----------------------------


def _tamper(setups, id, change):
    path = setups.dir / f"{id}.json"
    data = json.loads(path.read_text())
    change(data)
    path.write_text(json.dumps(data))


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda d: d["ruleset"].update(bypass_actors=[{"actor_id": 1, "actor_type": "OrganizationAdmin"}]),
         "Ruleset bypass_actors must be an empty list: nobody may bypass it."),
        (lambda d: d["ruleset"].update(enforcement="evaluate"), "Ruleset enforcement must be 'active'"),
        (lambda d: d["ruleset"].update(extra=1), "Ruleset has keys the gate does not set: 'extra'."),
        (lambda d: d.update(topics=["Bad Topic"]), "topic 'Bad Topic' must be lowercase letters"),
        (lambda d: d.update(topics=[]), "topics must hold 1 to 20 names, not 0."),
        (lambda d: d.update(repo="me/app.git"), "'me/app.git' is not an owner/name repository."),
        (lambda d: d.update(repo="../x"), "'../x' is not an owner/name repository."),
        (lambda d: d.update(description="two\nlines"), "description must be one line of printable characters"),
        (lambda d: d.update(description=" padded "), "description ' padded ' is not as repo_setup checks it."),
        (lambda d: d.update(secrets=["GITHUB_X"]), "must not start with GITHUB_"),
        (lambda d: d.update(secrets=["lower"]), "secret names ('lower',) are not as repo_setup checks them."),
        (lambda d: d.update(handover=0), "handover 0 is not a handover id."),
    ],
    ids=["bypass actors", "enforcement", "extra key", "bad topic", "no topics", "repo .git", "repo dots",
         "two-line description", "untrimmed description", "reserved secret", "lower-case secret", "handover 0"],
)  # fmt: skip
def test_a_tampered_record_is_refused_on_load(tmp_path, change, reason):
    setups = Setups(tmp_path)
    s = setups.create("me/app", "A thing", ("a",), TEMPLATE, secrets=("RELEASE_TOKEN",), handover=3)
    assert setups.get(s.id) == s
    _tamper(setups, s.id, change)
    with pytest.raises(Refused) as e:
        setups.get(s.id)
    assert str(e.value).startswith(f"Setup {s.id} is unreadable: ")
    assert reason in str(e.value)


def test_a_run_holds_the_setups_lock(tmp_path):
    setups = Setups(tmp_path)
    s = setups.create("me/app", "A thing", ("a",), TEMPLATE)
    lock = setups.dir / f"{s.id}.lock"
    with setups.running(s.id):
        assert stat.S_IMODE(lock.stat().st_mode) == 0o600
        with pytest.raises(Refused, match=f"^Setup {s.id} is already running.$"), Setups(tmp_path).running(s.id):
            pass
    with Setups(tmp_path).running(s.id):  # released on the way out
        pass
    setups.close(s.id)
    assert not lock.exists()


def test_a_run_lock_is_held_across_processes(tmp_path):
    import subprocess
    import sys

    setups = Setups(tmp_path)
    s = setups.create("me/app", "A thing", ("a",), TEMPLATE)
    holder = subprocess.Popen(
        [sys.executable, "-c", "import sys, time\nfrom pathlib import Path\nfrom git_gate.setup import Setups\n"
         f"with Setups(Path({str(tmp_path)!r})).running({s.id}):\n    print('held', flush=True)\n"
         "    sys.stdin.readline()\n"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )  # fmt: skip
    try:
        assert holder.stdout.readline() == "held\n"
        with pytest.raises(Refused, match="already running"), setups.running(s.id):
            pass
    finally:
        holder.communicate("\n", timeout=10)
    with setups.running(s.id):
        pass


def test_a_setup_with_a_first_push_expires_with_its_handover(env):
    _no_github(env)
    bundle, _, _ = _first_push(env)
    env.gate.dispatch("repo_setup", _request(env, bundle=str(bundle)))
    assert Setups(env.gate.state).get(1).created == Handovers(env.gate.state).get(1).created
