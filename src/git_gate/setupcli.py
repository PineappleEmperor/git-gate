"""gitgatesetup: apply a repo setup an agent asked for with `repo_setup`, as the user, outside the sandbox.

The user signs in to their own setup GitHub App through the device flow; the token lives in this
process only and is never printed or logged. The command shows how GitHub's live settings differ
from the request, asks, applies over REST, reads every item back, and closes the request only when
all of it took. A ruleset waits until the default branch exists, so the push that creates it is not
refused by the ruleset's own required checks. When the request holds a kept first push of main, that
push is applied in the same run, over the user's own SSH key as gitgatepush pushes it, and the
ruleset after it; a branch that already exists is never pushed over.
"""

import contextlib
import functools
import json
import signal
import sys
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from git_gate import Refused
from git_gate.appreg import load_app, register
from git_gate.call import audit
from git_gate.config import state_dir
from git_gate.devicelogin import NOT_CONFIRMED, Login, device_login
from git_gate.devicelogin import revoke as revoke_tokens
from git_gate.github import GitHub, GitHubError
from git_gate.gitops import Git
from git_gate.handover import EXPIRES, ID_RE, RULES, Gone, Handover, Handovers, _clean, push_kept, screen
from git_gate.mirror import Mirror
from git_gate.setup import SetupRequest, Setups

USAGE = "usage: gitgatesetup [<id> | --register]"
SET = "already set"
NOT_INSTALLED = (403, 404)  # a write the app may not make: not installed on the repo, or lacking a permission
FIRST = "main"  # the branch a kept first push creates
HANGUPS = (signal.SIGHUP, signal.SIGTERM)  # end a run as Ctrl-C does
SWAPPING = {*HANGUPS, signal.SIGINT}  # held back while the hangup handlers go in or come out
LAPSE = 300  # seconds before a setup's expiry from which its gone handover counts as expired, not pushed


@dataclass
class Item:
    name: str
    line: str  # "<current> → <wanted>", SET, or "deferred: <reason>"
    apply: Callable[[], object] | None = None  # None when there is nothing to do now


@dataclass
class Survey:
    """GitHub's live state against the request, one item per setting, in the order they are applied."""

    items: list[Item] = field(default_factory=list)
    default: str | None = None  # the repository's default branch, once the repository has been read
    ruleset_id: int | None = None  # the requested ruleset's id, when it is already set
    seen: dict[str, bool] = field(default_factory=dict)  # branches read from GitHub in this survey

    def has_branch(self, gh: GitHub, repo: str, name: str) -> bool:
        """Whether `name` has commits on GitHub; read once per survey."""
        if name not in self.seen:
            self.seen[name] = gh.branch(repo, name) is not None
        return self.seen[name]


class KeptPushes(Protocol):
    """The push handovers `repo_setup` kept for a request's first push of main."""

    def open(self, id: int, repo: str) -> str | None:
        """The kept commit when handover `id` is still open and is a first push of main to `repo`. None when
        its record is gone; Refused, with the reason, when it is unreadable or is not that push."""

    def pushed(self, id: int) -> bool:
        """Whether handover `id` was pushed: its record is gone. A record purged on expiry is gone too;
        the survey tells that apart by the setup's own age, since a setup expires with its handover."""

    def screen(self, id: int) -> str:
        """What gitgatepush shows for handover `id` before its prompt."""

    def push(self, id: int, say) -> None:
        """Push handover `id` as gitgatepush does; raises Refused when it does not go out."""


class _Kept:
    """The kept pushes in the gate's state, pushed with the code gitgatepush runs. Only the record
    `open` read is screened and pushed: a record that changes after it is refused."""

    def __init__(self, state: Path):
        self.state = state
        self.handovers = Handovers(state)
        self.records: dict[int, Handover] = {}

    @functools.cached_property
    def mirror(self) -> Mirror:  # built on first use: Git creates its directories under the state
        return Mirror(Git(self.state, "https://github.com"), self.state)

    def open(self, id: int, repo: str) -> str | None:
        self.records.pop(id, None)
        try:
            h = self.handovers.get(id)
        except Gone:
            return None
        if (h.repo, h.branch, h.base, h.lease) != (repo, FIRST, None, None):
            # Not the first push repo_setup keeps: gitgatepush's to show and run.
            raise Refused(f"Handover {id} is not a first push of {FIRST} to {repo}.")
        self.records[id] = h
        return h.sha

    def pushed(self, id: int) -> bool:
        return not self.handovers.dir.joinpath(f"{id}.json").exists()

    def _record(self, id: int) -> Handover:
        if id not in self.records:
            raise Refused(f"No open handover {id}.")
        return self.records[id]

    def screen(self, id: int) -> str:
        return screen(self._record(id), self.mirror, RULES)

    def push(self, id: int, say) -> None:
        push_kept(self.handovers, id, self.mirror, RULES, say, shown=self._record(id), tool="gitgatesetup")


def main(
    argv: list[str], *, stdin=sys.stdin, stdout=sys.stdout, login=device_login, github=GitHub,
    kept: KeptPushes | None = None, revoke=revoke_tokens,
) -> int:  # fmt: skip
    signed_in: list[Login] = []  # every sign-in of this run, handed over the moment it exists

    def say(text: str = "", end: str = "\n", *, lines: bool = False) -> None:
        for done in signed_in:
            for secret in filter(None, (done.token, done.refresh)):
                text = text.replace(secret, "[token]")
        try:
            stdout.write(_clean(text, lines=lines) + end)
            stdout.flush()
        except OSError, ValueError:  # a hung-up terminal or a closed stdout: the run still revokes and audits
            pass

    # While a run lasts, SIGHUP (a closed terminal) and SIGTERM raise KeyboardInterrupt, so the run's
    # finally blocks still revoke its token and audit it; a signal already ignored (nohup) stays ignored.
    # Hangups and Ctrl-C are held back (pthread_sigmask) while the handlers go in and come out: one that
    # comes while they go in interrupts once all are in, and one that comes while they come out reaches
    # the handler put back. The restore cannot be skipped: its block is retried until it holds.
    ran: list[int] = []  # the run's exit code, fixed before it audits: an interrupt after that is dropped
    previous: dict[int, object] = {}
    try:
        held = signal.pthread_sigmask(signal.SIG_BLOCK, ())  # the mask as it is; an interrupt here changes nothing
        try:
            try:
                signal.pthread_sigmask(signal.SIG_BLOCK, SWAPPING)  # may run a pending Ctrl-C: the finally unmasks
                _hangups_in(previous)
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, held)  # one that came meanwhile interrupts now
            code = _main(argv, stdin, say, login, github, kept, revoke, signed_in, ran)
        finally:
            while True:  # inline, first in the finally: nothing can interrupt before the retry loop starts
                try:
                    signal.pthread_sigmask(signal.SIG_BLOCK, SWAPPING)  # runs a pending handler, which may raise
                    break
                except KeyboardInterrupt:  # the run has ended, or never began: dropping it is right
                    pass
            _hangups_out(previous, held)
    except KeyboardInterrupt:  # outside the run's own handling: before it began, or after its code was fixed
        if ran:
            return ran[0]
        say()
        say("Interrupted.")
        return 1
    return code


def _hangups_in(previous: dict[int, object]) -> None:
    """Install the hangup handlers, with every signal in SWAPPING blocked; `previous` gets what they replace."""
    with contextlib.suppress(ValueError):  # not the main thread: signals stay as they are
        for signum in HANGUPS:
            if signal.getsignal(signum) is not signal.SIG_IGN:  # nohup's ignored hangup stays ignored
                previous[signum] = signal.signal(signum, _interrupt)


def _hangups_out(previous: dict[int, object], held) -> None:
    """Put the previous handlers back, with every signal in SWAPPING blocked, then the mask as it was."""
    try:
        for signum, handler in previous.items():
            signal.signal(signum, signal.SIG_DFL if handler is None else handler)
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, held)  # one that came meanwhile goes to the handler put back


def _main(
    argv: list[str], stdin, say, login, github, kept: KeptPushes | None, revoke, signed_in: list[Login], ran: list[int]
) -> int:
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
        return _run(state, int(argv[0]), stdin, say, login, github, kept or _Kept(state), signed_in, revoke, ran)
    except Refused as e:
        say(str(e), lines=True)
        return 1
    except KeyboardInterrupt:
        if ran:  # the run's code was fixed before its audit line: an interrupt after that changes nothing
            return ran[0]
        say()
        say("Interrupted.")
        return 1


def _interrupt(signum, frame) -> None:
    raise KeyboardInterrupt


def _list(setups: Setups, say) -> int:
    """The open setups; one whose record does not parse or fails its checks is named by id alone."""
    found = setups.listed()
    if not found:
        say("no open setups")
    for id, s in found:
        say(f"{id}: (unreadable)" if s is None else f"{s.id}  {s.repo}  {s.description}")
    return 0


def _run(
    state: Path, id: int, stdin, say, login, github, kept: KeptPushes, signed_in: list[Login], revoke, ran: list[int]
) -> int:  # fmt: skip
    """One run of `gitgatesetup <id>`, audited whatever its end, holding the setup's lock throughout.
    Every sign-in is revoked at the end, after the run's last GitHub call."""
    setups = Setups(state)
    repo, outcome, interrupted = None, "refused", False
    with contextlib.ExitStack() as held:
        try:
            repo = setups.get(id).repo  # an unknown id is refused before a lock file is made for it
            held.enter_context(setups.running(id))
            s = setups.get(id)  # read again under the lock: a run that just ended may have closed it
            outcome = _setup(setups, s, stdin, say, login, github, kept, signed_in)
            return 0 if outcome == "closed" else 1
        except KeyboardInterrupt:  # the run's own end: said last, once it has revoked and audited
            interrupted = outcome != "closed"  # one after the setup closed changes nothing
            return 0 if outcome == "closed" else 1
        finally:
            revoked = None
            try:
                if signed_in:
                    revoked = False  # until revoke says otherwise: never null after a sign-in
                    revoked = revoke(signed_in[0], alive=functools.partial(_alive, github), say=say)
            except BaseException as e:  # revoke never ends the run itself, Ctrl-C included: its outcome stays
                revoked = False
                say(f"Could not confirm the revocation ({type(e).__name__}).")
                say(NOT_CONFIRMED)
            finally:
                mask = None  # inline, first in this finally: hangups and Ctrl-C are held while the line is written
                while True:
                    try:
                        if mask is None:
                            mask = signal.pthread_sigmask(signal.SIG_BLOCK, ())  # the mask as it is
                        signal.pthread_sigmask(signal.SIG_BLOCK, SWAPPING)  # may run a pending handler, which raises
                        break
                    except KeyboardInterrupt:  # the run's outcome is fixed: dropping it is right
                        pass
                try:
                    ran.append(0 if outcome == "closed" else 1)  # fixed before the audit line
                    _audit(state, id, repo, outcome, revoked, say)
                    if interrupted:
                        say()
                        say("Interrupted.")
                finally:
                    # One held meanwhile lands here, after the mask is back; dropping it keeps the run's own
                    # return or error (a Refused on its way out still prints its line).
                    with contextlib.suppress(KeyboardInterrupt):
                        signal.pthread_sigmask(signal.SIG_SETMASK, mask)


def _alive(github, token: str) -> bool:
    """Whether GitHub still accepts `token`; only a 401 says it does not."""
    try:
        github(token).viewer()
    except GitHubError as e:
        return e.status != 401
    except Refused:  # a network blip or an answer without a message: not proof either way
        return True
    return True


def _setup(setups: Setups, s: SetupRequest, stdin, say, login, github, kept: KeptPushes,
           signed_in: list[Login]) -> str:  # fmt: skip
    app = load_app()
    done = login(app.client_id, say=say, got=signed_in.append)  # handed over before login returns
    if done not in signed_in:
        signed_in.append(done)
    gh = github(done.token)
    me = str(gh.viewer())
    if s.repo.split("/")[0].lower() != me.lower():
        raise Refused(
            f"{s.repo} is not under your account ({_clean(me)}); gitgatesetup sets up only your own repositories."
        )

    plan = _survey(gh, s, kept, say, clock=setups.clock)
    try:
        return _apply(setups, s, gh, kept, plan, stdin, say, app.slug)
    finally:
        _remind(s, say)


def _apply(setups: Setups, s: SetupRequest, gh: GitHub, kept: KeptPushes, plan: Survey, stdin, say, slug: str) -> str:
    """Show the plan, ask, apply, survey again, and close the request when nothing is left. Once the
    first change has started, an interrupt ends the run "interrupted", with the setup open."""
    say(f"Setup {s.id}: {s.repo}")
    for item in plan.items:
        say(f"  {item.name}: {item.line}", lines=True)  # every live value in a line is cleaned already
    todo = [item for item in plan.items if item.apply]
    if s.handover is not None and any(item.name == FIRST for item in todo):
        say()
        say(kept.screen(s.handover), lines=True)
    if todo:
        say("Apply? [y/N] ", end="")
        if not _yes(stdin, say):
            say(f"Not applied. Setup {s.id} stays open.")
            return "declined"
        accepted: set[str] = set()
        closed: list[int] = []  # the setup's id once _verify has closed it
        try:  # GitHub may hold a change from here on, even from an item interrupted part-way
            for item in todo:
                try:
                    item.apply()
                    accepted.add(item.name)
                except Refused as e:
                    if isinstance(e, GitHubError) and (e.status in NOT_INSTALLED or _name_taken(item, e)):
                        say(f"GitHub refused {item.name} ({e.status}). The app may not be installed on this "
                            f"repository or may lack a permission; install it on all repositories: "
                            f"https://github.com/apps/{slug}/installations/new")  # fmt: skip
                    else:
                        say(f"Applying {item.name} failed: {e}")
                    say(f"Setup {s.id} stays open. Re-run: gitgatesetup {s.id}")
                    return "open"
            plan = _survey(gh, s, kept, say, frozenset(accepted), clock=setups.clock)
            return _verify(setups, s, gh, plan, say, closed)
        except Refused as e:  # the read-back failed after GitHub took changes: open, to re-run
            say(f"Reading the changes back failed: {e}", lines=True)
            say(f"Setup {s.id} stays open. Re-run: gitgatesetup {s.id}")
            return "open"
        except KeyboardInterrupt:
            say()
            say("Interrupted.")
            if closed:  # everything verified and the setup closed just before the interrupt
                return "closed"
            say(f"Re-run: gitgatesetup {s.id}")
            return "interrupted"
    return _verify(setups, s, gh, plan, say, [])


def _verify(setups: Setups, s: SetupRequest, gh: GitHub, plan: Survey, say, closed: list[int]) -> str:
    """Name what is left, or close the request when nothing is; `closed` gets its id once it is."""
    _others(gh, s, plan, say)
    left = [item for item in plan.items if item.line != SET]
    if left:
        say("Not done yet:")
        for item in left:
            say(f"  {item.name}: {item.line}", lines=True)  # every live value in a line is cleaned already
        say(f"Re-run: gitgatesetup {s.id}")
        return "open"
    setups.close(s.id)
    closed.append(s.id)
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


def _survey(
    gh: GitHub, s: SetupRequest, kept: KeptPushes, say, accepted: frozenset[str] = frozenset(), *,
    clock: Callable[[], float] = time.time,
) -> Survey:  # fmt: skip
    """Read GitHub's live state for every item of `s`. Every value read from GitHub is cleaned before
    it is shown, and an item's `apply` is set only when it differs from the request. `accepted` names
    the items GitHub took a change for in this run. An absent repository has no main. `clock` is the
    setups' clock, against which the setup's age is read."""
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
    first = _first_push(gh, s, live, kept, say, out)
    if first:
        out.items.append(first)
    # A setup expires with its handover, so a gone handover of a setup about to expire may have expired.
    lapsed = first is None and s.handover is not None and clock() - s.created > EXPIRES - LAPSE
    pushed = first is None and s.handover is not None and not lapsed and kept.pushed(s.handover)
    out.items.append(_ruleset(gh, s, live, out, pending=bool(first and first.apply), pushed=pushed, lapsed=lapsed))
    return out


def _graph(gh: GitHub, repo: str, on: bool | None, accepted: frozenset[str]) -> Item:
    """The dependency graph. Its status may be unreadable to the app; then the enabling PATCH is sent,
    and GitHub accepting it is the verification, so an unreadable status never blocks closing."""
    if on or (on is None and "dependency graph" in accepted):
        return Item("dependency graph", SET)
    current = "unknown" if on is None else "disabled"
    return Item("dependency graph", f"{current} → enabled", lambda: gh.enable_dependency_graph(repo))


def _first_push(gh: GitHub, s: SetupRequest, live: dict | None, kept: KeptPushes, say, seen: Survey) -> Item | None:
    """The kept first push of main. A main that exists is never pushed over, whatever its commit; a
    handover that is gone (pushed by gitgatepush, or expired) leaves no item."""
    if s.handover is None:
        return None
    if live is not None and seen.has_branch(gh, s.repo, FIRST):
        return Item(FIRST, SET)
    try:
        sha = kept.open(s.handover, s.repo)
    except Refused as e:  # unreadable or not this setup's push: named, and left for gitgatepush
        return Item(FIRST, f"the handover cannot be used: {_clean(str(e))}")
    if not sha:
        return None
    line = f"absent → push {_clean(sha[:12])} (handover {s.handover}; shown below)"
    return Item(FIRST, line, functools.partial(kept.push, s.handover, say))


def _default(gh: GitHub, s: SetupRequest, live: dict | None, seen: Survey) -> tuple[str | None, bool]:
    """The repository's default branch (None when there is no repository) and whether it has commits."""
    default = str(live.get("default_branch") or FIRST) if live else None
    return default, default is not None and seen.has_branch(gh, s.repo, default)


def _same_name(gh: GitHub, s: SetupRequest) -> int | None:
    """The id of this repository's own branch ruleset named as the request's, if there is one."""
    wanted = (s.ruleset.get("name"), "branch", "Repository")
    ids = [r.get("id") for r in gh.rulesets(s.repo) if (r.get("name"), r.get("target"), r.get("source_type")) == wanted]
    return next((i for i in ids if isinstance(i, int) and not isinstance(i, bool)), None)


def _apply_ruleset(gh: GitHub, s: SetupRequest) -> None:
    """Apply the ruleset once the first push has landed. It writes nothing when the default branch
    still has no commits (it is not main); the survey after applying then shows it deferred."""
    if not _default(gh, s, gh.repo(s.repo), Survey())[1]:  # read afresh: the push has just landed
        return
    id = _same_name(gh, s)
    if id is None:
        gh.create_ruleset(s.repo, s.ruleset)
    else:
        gh.update_ruleset(s.repo, id, s.ruleset)


def _ruleset(
    gh: GitHub, s: SetupRequest, live: dict | None, out: Survey, *, pending: bool = False, pushed: bool = False,
    lapsed: bool = False,
) -> Item:  # fmt: skip
    """The ruleset; `pending` when a first push of main is applied before it in this run. Then the
    ruleset is applied after the push (by `_apply_ruleset`), but only when main is or may be the
    default branch: a push of main does not give another default branch its first commit. `pushed`
    when the request's first push already went out (its handover is closed), though GitHub may not
    show main yet; `lapsed` when its handover is gone and has, or may have, expired unpushed."""
    name = s.ruleset.get("name")
    default, exists = _default(gh, s, live, out)
    out.default = default
    after = ""
    if not exists:
        if lapsed and default in (None, FIRST):
            return Item("ruleset", f"deferred: {FIRST} does not exist yet, and handover {s.handover} with its first "
                                   f"push has expired; ask the agent for a new repo_setup")  # fmt: skip
        if pushed and default == FIRST:
            return Item("ruleset", f"deferred: {FIRST} was pushed; GitHub does not show it yet. "
                                   f"Re-run in a moment: gitgatesetup {s.id}")  # fmt: skip
        if not pending or (default is not None and default != FIRST):
            branch = _clean(default) if default else "the default branch"
            return Item("ruleset", f"deferred: {branch} does not exist yet; push it (gitgatepush), "
                                   f"then re-run gitgatesetup {s.id}")  # fmt: skip
        after = f" after {FIRST} is pushed"
        if live is None:  # a repository created in this run has no rulesets
            line = f"absent → {_quote(str(name))}{after}; rules: {_rules(s.ruleset)}"
            return Item("ruleset", line, lambda: _apply_ruleset(gh, s))
    id = _same_name(gh, s)
    if id is None:
        line = f"absent → {_quote(str(name))}{after}; rules: {_rules(s.ruleset)}"
        create = (lambda: _apply_ruleset(gh, s)) if after else (lambda: gh.create_ruleset(s.repo, s.ruleset))
        return Item("ruleset", line, create)
    have = gh.ruleset(s.repo, id)
    replace = (lambda: _apply_ruleset(gh, s)) if after else (lambda: gh.update_ruleset(s.repo, id, s.ruleset))
    if not _readable(have):
        line = f"{_quote(str(name))} (id {id}) differs → replace it{after}. rules: unreadable → {_rules(s.ruleset)}"
        line += "\n    GitHub's copy has an unexpected shape; it is replaced whole."
        return Item("ruleset", line, replace)
    if _subset(s.ruleset, have):
        out.ruleset_id = id
        return Item("ruleset", SET)
    line = f"{_quote(str(name))} (id {id}) differs → replace it{after}. rules: {_rules(have)} → {_rules(s.ruleset)}"
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
    return Item("ruleset", line, replace)


def _readable(ruleset: object) -> bool:
    """Whether a ruleset GitHub answered has the shape gitgatesetup compares and shows: rules a list of
    objects with a text type, parameters an object, required checks a list of objects with a text
    context, conditions.ref_name an object whose include and exclude are lists of text, and
    bypass_actors a list of objects. Absent parameters, patterns and bypass_actors are fine."""
    if not isinstance(ruleset, dict):
        return False
    rules = ruleset.get("rules")
    if not isinstance(rules, list) or not all(isinstance(r, dict) and isinstance(r.get("type"), str) for r in rules):
        return False
    for rule in rules:
        params = rule.get("parameters", {})
        checks = params.get("required_status_checks", []) if isinstance(params, dict) else None
        if not isinstance(checks, list) or not all(isinstance(c, dict) and isinstance(c.get("context"), str)
                                                   for c in checks):  # fmt: skip
            return False
    conditions = ruleset.get("conditions")
    ref_name = conditions.get("ref_name") if isinstance(conditions, dict) else None
    if not isinstance(ref_name, dict):
        return False
    for key in ("include", "exclude"):
        refs = ref_name.get(key, [])
        if not isinstance(refs, list) or not all(isinstance(r, str) for r in refs):
            return False
    actors = ruleset.get("bypass_actors", [])
    return isinstance(actors, list) and all(isinstance(a, dict) for a in actors)


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


def _remind(s: SetupRequest, say) -> None:
    """The secrets reminder. An interrupt cuts it short but leaves the run's outcome as it was."""
    try:
        _secrets(s, say)
    except KeyboardInterrupt:
        say()
        say("Interrupted.")


def _secrets(s: SetupRequest, say) -> None:
    """Name the Actions secrets the request asked for. The app cannot read secrets, so a reminder
    never keeps a setup open."""
    if not s.secrets:
        return
    say("Add these Actions secrets yourself on GitHub (gitgatesetup cannot check them):")
    for name in s.secrets:
        say(f"  {_clean(name)}: https://github.com/{_clean(s.repo)}/settings/secrets/actions/new")


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


def _audit(state: Path, id: int, repo: str | None, outcome: str, revoked: bool | None, say) -> None:
    """One audit.log line per `gitgatesetup <id>` run. The id says which request; no description, no token."""
    line = json.dumps({
        "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "tool": "gitgatesetup", "id": id, "repo": repo,
        "outcome": outcome, "revoked": revoked,
    })  # fmt: skip
    try:
        audit(state, line)
    except OSError as e:
        say(f"Warning: could not write {state / 'audit.log'}: {e.strerror}.")
