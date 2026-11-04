"""gitgatesetup: apply a repo setup an agent asked for with `repo_setup`, as the user, outside the sandbox.

The user signs in to their own setup GitHub App through the device flow; the token lives in this
process only and is never printed or logged. The command shows how GitHub's live settings differ
from the request, asks, applies over REST, reads every item back, and closes the request only when
all of it took. A ruleset waits until the default branch exists, so the push that creates it is not
refused by the ruleset's own required checks.
"""

import functools
import json
import sys
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from git_gate import Refused
from git_gate.appreg import load_app, register
from git_gate.call import audit
from git_gate.config import state_dir, valid_repo
from git_gate.devicelogin import device_login
from git_gate.github import GitHub, GitHubError
from git_gate.handover import ID_RE, _clean
from git_gate.setup import SetupRequest, Setups

USAGE = "usage: gitgatesetup [<id> | --register]"
SET = "already set"
NOT_INSTALLED = (403, 404)  # a write the app may not make: not installed on the repo, or lacking a permission


@dataclass
class Item:
    name: str
    line: str  # "<current> → <wanted>", SET, or "deferred: <reason>"
    apply: Callable[[], object] | None = None  # None when there is nothing to do now


@dataclass
class Survey:
    """GitHub's live state against the request, one item per setting, in the order they are applied."""

    items: list[Item] = field(default_factory=list)
    default: str | None = None  # the default branch, when it exists
    ruleset_id: int | None = None  # the requested ruleset's id, when it is already set


def main(argv: list[str], *, stdin=sys.stdin, stdout=sys.stdout, login=device_login, github=GitHub) -> int:
    secrets: list[str] = []

    def say(text: str = "", end: str = "\n", *, lines: bool = False) -> None:
        for secret in secrets:
            text = text.replace(secret, "[token]")
        stdout.write(_clean(text, lines=lines) + end)
        stdout.flush()

    try:
        state = state_dir()
        if not argv:
            return _list(Setups(state), say)
        if argv == ["--register"]:
            register(say=say)
            return 0
        if len(argv) != 1 or not ID_RE.fullmatch(argv[0]):
            say(USAGE)
            return 2
        return _run(state, int(argv[0]), stdin, say, login, github, secrets)
    except Refused as e:
        say(str(e), lines=True)
        return 1
    except KeyboardInterrupt:
        say()
        say("Interrupted.")
        return 1


def _list(setups: Setups, say) -> int:
    found = setups.open()
    if not found:
        say("no open setups")
    for s in found:
        say(f"{s.id}  {s.repo}  {s.description}")
    return 0


def _run(state: Path, id: int, stdin, say, login, github, secrets: list[str]) -> int:
    """One run of `gitgatesetup <id>`, audited whatever its end."""
    setups = Setups(state)
    repo, outcome = None, "refused"
    try:
        s = setups.get(id)
        repo = s.repo
        outcome = _setup(setups, s, stdin, say, login, github, secrets)
        return 0 if outcome == "closed" else 1
    finally:
        _audit(state, id, repo, outcome, say)


def _setup(setups: Setups, s: SetupRequest, stdin, say, login, github, secrets: list[str]) -> str:
    if not valid_repo(s.repo):
        raise Refused(f"Setup {s.id} names a bad repository.")
    app = load_app()
    token = login(app.client_id, say=say)
    secrets.append(token)
    gh = github(token)
    me = str(gh.viewer())
    if s.repo.split("/")[0].lower() != me.lower():
        raise Refused(
            f"{s.repo} is not under your account ({_clean(me)}); gitgatesetup sets up only your own repositories."
        )

    plan = _survey(gh, s)
    say(f"Setup {s.id}: {s.repo}")
    for item in plan.items:
        say(f"  {item.name}: {item.line}", lines=True)  # every live value in a line is cleaned already
    todo = [item for item in plan.items if item.apply]
    if todo:
        say("Apply? [y/N] ", end="")
        if not _yes(stdin, say):
            say(f"Not applied. Setup {s.id} stays open.")
            return "declined"
        accepted: set[str] = set()
        for item in todo:
            try:
                item.apply()
                accepted.add(item.name)
            except KeyboardInterrupt:
                if not accepted:
                    raise
                say()
                say("Interrupted.")
                say(f"Re-run: gitgatesetup {s.id}")
                return "open"
            except Refused as e:
                if isinstance(e, GitHubError) and (e.status in NOT_INSTALLED or _name_taken(item, e)):
                    say(f"GitHub refused {item.name} ({e.status}). The app may not be installed on this repository "
                        f"or may lack a permission; install it on all repositories: "
                        f"https://github.com/apps/{app.slug}/installations/new")  # fmt: skip
                else:
                    say(f"Applying {item.name} failed: {e}")
                say(f"Setup {s.id} stays open. Re-run: gitgatesetup {s.id}")
                return "open"
        plan = _survey(gh, s, frozenset(accepted))

    _others(gh, s, plan, say)
    left = [item for item in plan.items if item.line != SET]
    if left:
        say("Not done yet:")
        for item in left:
            say(f"  {item.name}: {item.line}", lines=True)  # every live value in a line is cleaned already
        say(f"Re-run: gitgatesetup {s.id}")
        return "open"
    setups.close(s.id)
    say(f"Done. Setup {s.id} is closed.")
    return "closed"


def _name_taken(item: Item, e: GitHubError) -> bool:
    """Creating the repository failed because the name is taken: a private repo the app cannot see."""
    return item.name == "repository" and e.status == 422 and "already exists" in str(e).lower()


def _yes(stdin, say) -> bool:
    """The answer to the prompt; Ctrl-C or end of input counts as no."""
    try:
        answer = stdin.readline()
    except KeyboardInterrupt, EOFError:
        answer = ""
    if not answer.endswith("\n"):
        say()
    return answer.strip().lower() in ("y", "yes")


def _survey(gh: GitHub, s: SetupRequest, accepted: frozenset[str] = frozenset()) -> Survey:
    """Read GitHub's live state for every item of `s`. Every value read from GitHub is cleaned before
    it is shown, and an item's `apply` is set only when it differs from the request. `accepted` names
    the items GitHub took a change for in this run."""
    repo, out = s.repo, Survey()
    live = gh.repo(repo)
    if live is None:
        name = repo.split("/")[1]
        create = functools.partial(gh.create_user_repo, name, s.description)
        out.items.append(Item("repository", f"absent (or not visible to the setup app) → create public {repo}", create))
        # The repo is created with the description and Issues on, so those two are not sent again.
        out.items.append(Item("description", f"(none) → {_quote(s.description)}"))
        out.items.append(Item("Issues", "off → on"))
        topics, graph = [], False
    else:
        out.items.append(Item("repository", SET))
        description = live.get("description") or ""
        out.items.append(_item("description", description == s.description, _quote(description), _quote(s.description),
                               lambda: gh.edit_repo(repo, description=s.description)))  # fmt: skip
        out.items.append(
            _item("Issues", live.get("has_issues") is True, "off", "on", lambda: gh.edit_repo(repo, has_issues=True))
        )
        topics, graph = gh.topics(repo), gh.dependency_graph_on(repo, live)
    out.items.append(_item("topics", set(topics) == set(s.topics), _names(topics), _names(s.topics),
                           lambda: gh.set_topics(repo, s.topics)))  # fmt: skip
    out.items.append(_graph(gh, repo, graph, accepted))
    out.items.append(_ruleset(gh, s, live, out))
    return out


def _graph(gh: GitHub, repo: str, on: bool | None, accepted: frozenset[str]) -> Item:
    """The dependency graph. Its status may be unreadable to the app; then the enabling PATCH is sent,
    and GitHub accepting it is the verification, so an unreadable status never blocks closing."""
    if on or (on is None and "dependency graph" in accepted):
        return Item("dependency graph", SET)
    current = "unknown" if on is None else "disabled"
    return Item("dependency graph", f"{current} → enabled", lambda: gh.enable_dependency_graph(repo))


def _ruleset(gh: GitHub, s: SetupRequest, live: dict | None, out: Survey) -> Item:
    default = str(live.get("default_branch") or "main") if live else None
    if default is None or gh.branch(s.repo, default) is None:
        branch = _clean(default) if default else "the default branch"
        return Item("ruleset", f"deferred: {branch} does not exist yet; push it (gitgatepush), "
                               f"then re-run gitgatesetup {s.id}")  # fmt: skip
    out.default = default
    name = s.ruleset.get("name")
    wanted = (name, "branch", "Repository")
    ids = [r.get("id") for r in gh.rulesets(s.repo) if (r.get("name"), r.get("target"), r.get("source_type")) == wanted]
    id = next((i for i in ids if isinstance(i, int) and not isinstance(i, bool)), None)
    if id is None:
        line = f"absent → {_quote(str(name))}; rules: {_rules(s.ruleset)}"
        return Item("ruleset", line, lambda: gh.create_ruleset(s.repo, s.ruleset))
    have = gh.ruleset(s.repo, id)
    if _subset(s.ruleset, have):
        out.ruleset_id = id
        return Item("ruleset", SET)
    line = f"{_quote(str(name))} (id {id}) differs → replace it. rules: {_rules(have)} → {_rules(s.ruleset)}"
    if _scope(have) != _scope(s.ruleset):
        line += f"\n    branches: {_scope(have)} → {_scope(s.ruleset)}"
    if have.get("enforcement") != s.ruleset.get("enforcement"):
        line += (
            f"\n    enforcement: {_clean(str(have.get('enforcement')))} → {_clean(str(s.ruleset.get('enforcement')))}"
        )
    if bypass := _bypass(have):
        line += f"\n    bypass actors: {bypass} → {_bypass(s.ruleset) or 'none'}"
    if lost := _removed(have, s.ruleset):
        line += f"\n    removes: {lost}"
    return Item("ruleset", line, lambda: gh.update_ruleset(s.repo, id, s.ruleset))


def _others(gh: GitHub, s: SetupRequest, plan: Survey, say) -> None:
    """Name every other ruleset that also requires checks on the default branch. They are the user's
    to keep or remove; gitgatesetup never deletes one."""
    if plan.ruleset_id is None or plan.default is None:
        return
    found: dict[int, str] = {}
    for rule in gh.branch_rule_rulesets(s.repo, plan.default):
        id = rule.get("ruleset_id")
        if not isinstance(id, int) or isinstance(id, bool) or id == plan.ruleset_id or id in found:
            continue
        if rule.get("ruleset_source_type") == "Organization":
            org = urllib.parse.quote(str(rule.get("ruleset_source") or s.repo.split("/")[0]), safe="")
            found[id] = f"https://github.com/organizations/{org}/settings/rules/{id}"
        else:
            found[id] = f"https://github.com/{s.repo}/settings/rules/{id}"
    if found:
        say(f"Other rulesets also require checks on {_clean(plan.default)}; review them (gitgatesetup leaves them):")
        for url in found.values():
            say(f"  {url}")


def _checks(ruleset: object) -> list[str]:
    """The required status check contexts of a ruleset, in order."""
    out: list[str] = []
    for rule in _rule_list(ruleset):
        if rule.get("type") == "required_status_checks":
            params = rule.get("parameters")
            for check in (params.get("required_status_checks") if isinstance(params, dict) else None) or []:
                out.append(str(check.get("context")) if isinstance(check, dict) else "?")
    return out


def _rule_list(ruleset: object) -> list[dict]:
    rules = ruleset.get("rules") if isinstance(ruleset, dict) else None
    return [r for r in rules if isinstance(r, dict)] if isinstance(rules, list) else []


def _rules(ruleset: object) -> str:
    """A ruleset's rule types in order; required_status_checks lists its contexts."""
    parts = [
        f"required_status_checks[{_names(_checks(ruleset))}]" if r.get("type") == "required_status_checks"
        else _clean(str(r.get("type")))
        for r in _rule_list(ruleset)
    ]  # fmt: skip
    return ", ".join(parts) or "(none)"


def _refs(ruleset: object, key: str) -> list[str]:
    """The `include` or `exclude` branch patterns of a ruleset's ref_name condition."""
    conditions = ruleset.get("conditions") if isinstance(ruleset, dict) else None
    ref_name = conditions.get("ref_name") if isinstance(conditions, dict) else None
    refs = ref_name.get(key) if isinstance(ref_name, dict) else None
    return [str(r) for r in refs] if isinstance(refs, list) else []


def _scope(ruleset: object) -> str:
    """Which branches a ruleset covers: its include patterns, then any exclude patterns."""
    exclude = _refs(ruleset, "exclude")
    return f"include {_names(_refs(ruleset, 'include'))}" + (f"; exclude {_names(exclude)}" if exclude else "")


def _bypass(ruleset: object) -> int:
    actors = ruleset.get("bypass_actors") if isinstance(ruleset, dict) else None
    return len(actors) if isinstance(actors, list) else 0


def _removed(have: object, want: object) -> str:
    """What replacing `have` with `want` drops: rule types, required check contexts, and branches it no
    longer covers (an include pattern gone, or an exclude pattern added)."""
    kept = {r.get("type") for r in _rule_list(want)}
    gone = [_clean(str(r.get("type"))) for r in _rule_list(have) if r.get("type") not in kept]
    wanted = _checks(want)
    gone += [f'check "{_clean(c)}"' for c in _checks(have) if c not in wanted]
    included, excluded = _refs(want, "include"), _refs(have, "exclude")
    gone += [f'branches "{_clean(r)}"' for r in _refs(have, "include") if r not in included]
    gone += [f'branches "{_clean(r)}" (newly excluded)' for r in _refs(want, "exclude") if r not in excluded]
    return ", ".join(gone)


def _item(name: str, done: bool, current: str, wanted: str, apply: Callable[[], object]) -> Item:
    return Item(name, SET) if done else Item(name, f"{current} → {wanted}", apply)


def _subset(want: object, have: object) -> bool:
    """Whether every key and value of `want` is in `have`, recursively; lists match element by element,
    in order. Keys GitHub adds (ids, source, a null integration_id, ...) are ignored."""
    if isinstance(want, dict):
        return isinstance(have, dict) and all(_subset(v, have.get(k)) for k, v in want.items())
    if isinstance(want, list):
        return isinstance(have, list) and len(want) == len(have) and all(map(_subset, want, have, strict=True))
    return type(want) is type(have) and want == have


def _quote(text: str) -> str:
    return f'"{_clean(text)}"' if text else "(none)"


def _names(names) -> str:
    return ", ".join(_clean(str(n)) for n in names) or "(none)"


def _audit(state: Path, id: int, repo: str | None, outcome: str, say) -> None:
    """One audit.log line per `gitgatesetup <id>` run. The id says which request; no description, no token."""
    line = json.dumps({
        "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "tool": "gitgatesetup", "id": id, "repo": repo,
        "outcome": outcome,
    })  # fmt: skip
    try:
        audit(state, line)
    except OSError as e:
        say(f"Warning: could not write {state / 'audit.log'}: {e.strerror}.")
