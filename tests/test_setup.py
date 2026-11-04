import copy
import json
import stat

import pytest

from git_gate import Refused
from git_gate.handover import EXPIRES
from git_gate.setup import Setups, check_description, check_ruleset, check_topics

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


@pytest.mark.parametrize("data", [b"not json", b"[]", b"\xff"])
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
    for bad in ("x" * 351, "a\nb", "", 5):
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
