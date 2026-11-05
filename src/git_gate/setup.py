"""Setup requests: a repo to create (or adjust) on GitHub, kept for the user to apply with `gitgatesetup <id>`.

The agent proposes a description, topics and a ruleset file; this module checks them and keeps them
as a record under <state>/setups/<id>.json. Nothing here calls GitHub.
"""

import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from git_gate import Refused
from git_gate.handover import EXPIRES, ID_RE, allocate_id
from git_gate.writes import _line, _show

MAX_DESCRIPTION = 350
MAX_TOPICS = 20
TOPIC_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,49}")  # fullmatch only
MAX_RULESET = 64 * 1024
MAX_RULESET_NAME = 100
RULESET_KEYS = {"name", "target", "enforcement", "conditions", "rules", "bypass_actors"}
REQUIRED_RULES = ("deletion", "non_fast_forward")
MAX_SECRETS = 10
SECRET_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,99}")  # fullmatch only


@dataclass(frozen=True)
class SetupRequest:
    id: int
    repo: str
    description: str
    topics: tuple[str, ...]
    ruleset: dict
    created: float
    handover: int | None = None
    secrets: tuple[str, ...] = ()


def check_description(value: object) -> str:
    text = _line(value, "description", MAX_DESCRIPTION)
    return text.strip()  # GitHub trims it, so an untrimmed one would never verify


def check_topics(value: object) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        raise Refused(f"topics must be a list of names, not {_show(value)}.")
    if not 1 <= len(value) <= MAX_TOPICS:
        raise Refused(f"topics must hold 1 to {MAX_TOPICS} names, not {len(value)}.")
    for topic in value:
        if not isinstance(topic, str) or not TOPIC_RE.fullmatch(topic):
            raise Refused(
                f"topic {_show(topic)} must be lowercase letters, digits and hyphens, "
                "start with a letter or digit, and be at most 50 characters."
            )
    if len(set(value)) != len(value):
        raise Refused("topics must not repeat a name.")
    return tuple(value)


def check_secrets(value: object) -> tuple[str, ...]:
    """The Actions secret names to remind the user of, upper-cased as GitHub stores them."""
    if value is None:
        return ()
    if not isinstance(value, list | tuple):
        raise Refused(f"secrets must be a list of names, not {_show(value)}.")
    if not 1 <= len(value) <= MAX_SECRETS:
        raise Refused(f"secrets must hold 1 to {MAX_SECRETS} names, not {len(value)}.")
    names = []
    for name in value:
        if not isinstance(name, str) or not SECRET_RE.fullmatch(name):
            raise Refused(
                f"secret name {_show(name)} must use letters, digits and underscores, "
                "not start with a digit, and be at most 100 characters."
            )
        if name.upper().startswith("GITHUB_"):
            raise Refused(f"secret name {_show(name)} must not start with GITHUB_; GitHub reserves that prefix.")
        names.append(name.upper())
    if len(set(names)) != len(names):
        raise Refused("secrets must not repeat a name.")
    return tuple(names)


def check_ruleset(data: bytes, path: str) -> dict:
    """The parsed ruleset when it is a plain branch ruleset for the default branch, else Refused."""
    if len(data) > MAX_RULESET:
        raise Refused(f"Ruleset file {path} is {len(data)} bytes; the limit is 64 KiB.")
    try:
        ruleset = json.loads(data)
    except ValueError, RecursionError:  # ValueError includes UnicodeDecodeError
        ruleset = None
    if not isinstance(ruleset, dict):
        raise Refused(f"Ruleset file {path} must hold one JSON object.")
    extra = sorted(ruleset.keys() - RULESET_KEYS)
    if extra:
        raise Refused(f"Ruleset has keys the gate does not set: {', '.join(map(_show, extra))}.")
    _line(ruleset.get("name"), "ruleset name", MAX_RULESET_NAME)
    if ruleset.get("target") != "branch":
        raise Refused(f"Ruleset target must be 'branch', not {_show(ruleset.get('target'))}.")
    if ruleset.get("enforcement") != "active":
        raise Refused(f"Ruleset enforcement must be 'active', not {_show(ruleset.get('enforcement'))}.")
    if ruleset.get("bypass_actors") != []:
        raise Refused("Ruleset bypass_actors must be an empty list: nobody may bypass it.")
    conditions = ruleset.get("conditions")
    ref_name = conditions.get("ref_name") if isinstance(conditions, dict) else None
    if not isinstance(ref_name, dict) or ref_name.get("include") != ["~DEFAULT_BRANCH"]:
        raise Refused('Ruleset conditions.ref_name.include must be ["~DEFAULT_BRANCH"].')
    rules = ruleset.get("rules")
    if not isinstance(rules, list) or not all(isinstance(r, dict) for r in rules):
        raise Refused("Ruleset rules must be a list of objects.")
    kinds = {r.get("type") for r in rules}
    for kind in REQUIRED_RULES:
        if kind not in kinds:
            raise Refused(f"Ruleset must hold a {kind} rule.")
    checks = [
        r.get("parameters", {}).get("required_status_checks")
        for r in rules
        if r.get("type") == "required_status_checks" and isinstance(r.get("parameters"), dict)
    ]
    if not any(isinstance(c, list) and c for c in checks):
        raise Refused("Ruleset must hold a required_status_checks rule with at least one required status check.")
    return ruleset


class Setups:
    """JSON records under <state>/setups/<id>.json, ids from the flock-guarded <state>/setups/next."""

    def __init__(self, state: Path, clock: Callable[[], float] = time.time):
        self.dir = state / "setups"
        self.clock = clock

    def create(
        self, repo: str, description: str, topics: tuple[str, ...], ruleset: dict, *,
        handover: int | None = None, secrets: tuple[str, ...] = (),
    ) -> SetupRequest:  # fmt: skip
        with allocate_id(self.dir, self._highest) as number:
            s = SetupRequest(number, repo, description, tuple(topics), ruleset, self.clock(), handover, tuple(secrets))
            self._write(s)
            self._purge()
        return s

    def get(self, id: int) -> SetupRequest:
        path = self._path(id)
        try:
            data = path.read_text()
        except FileNotFoundError:
            raise Refused(f"No open setup {id}.") from None
        except OSError as e:
            raise Refused(f"Setup {id} is unreadable: {e.strerror}.") from None
        s = _parse(data, id)
        if s is None:
            raise Refused(f"Setup {id} is unreadable: {path} is not a setup record.")
        if self._expired(s):
            raise Refused(f"No open setup {id}.")
        return s

    def open(self) -> list[SetupRequest]:
        return [s for s in self._all() if not self._expired(s)]

    def close(self, id: int) -> None:
        self._path(id).unlink(missing_ok=True)

    def _path(self, id: int) -> Path:
        if not isinstance(id, int) or isinstance(id, bool) or id < 1:
            raise Refused(f"{id!r} is not a setup id.")
        return self.dir / f"{id}.json"

    def _expired(self, s: SetupRequest) -> bool:
        return self.clock() - s.created > EXPIRES

    def _write(self, s: SetupRequest) -> None:
        path = self._path(s.id)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.new")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(asdict(s), f)
        tmp.replace(path)

    def _purge(self) -> None:
        for s in self._all():
            if self._expired(s):
                self.close(s.id)

    def _highest(self) -> int:
        """The highest id with a record file, readable or not."""
        ids = [int(p.stem) for p in self.dir.glob("*.json") if ID_RE.fullmatch(p.stem)]
        return max(ids, default=0)

    def _all(self) -> list[SetupRequest]:
        found = []
        for path in self.dir.glob("*.json") if self.dir.is_dir() else ():
            if ID_RE.fullmatch(path.stem):
                try:
                    s = _parse(path.read_text(), int(path.stem))
                except OSError:
                    continue
                if s:
                    found.append(s)
        return sorted(found, key=lambda s: s.id)


def _parse(text: str, id: int) -> SetupRequest | None:
    """A record, or None when it does not parse or any field has the wrong shape."""
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            return None
        if isinstance(data.get("topics"), list):
            data["topics"] = tuple(data["topics"])
        if isinstance(data.get("secrets"), list):
            data["secrets"] = tuple(data["secrets"])
        s = SetupRequest(**data)
    except ValueError, TypeError:
        return None
    ok = (
        s.id == id
        and isinstance(s.repo, str)
        and isinstance(s.description, str)
        and isinstance(s.topics, tuple)
        and all(isinstance(t, str) for t in s.topics)
        and isinstance(s.ruleset, dict)
        and isinstance(s.created, int | float)
        and (s.handover is None or (isinstance(s.handover, int) and not isinstance(s.handover, bool)))
        and isinstance(s.secrets, tuple)
        and all(isinstance(n, str) for n in s.secrets)
    )
    return s if ok else None
