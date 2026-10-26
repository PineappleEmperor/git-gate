"""Read-only CI status: whether a pull request, branch or commit passed its checks.

Agents learnt this from `gh pr checks` and `gh run watch`, whose requests are gh's own business:
`--watch` polls every few seconds on the shared token. These tools make a known, small set of
REST calls instead. Everything GitHub sends is printed as one printable line per field, and the
gate marks the whole reply as GitHub content.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from git_gate import Refused
from git_gate.config import Rules
from git_gate.github import FromGitHub, GitHub, GitHubError, RateLimited
from git_gate.gitops import Git

SHA = re.compile(r"[0-9a-fA-F]{7,40}")
PULL_REF = re.compile(r"#(\d+)")
ACTIONS_JOB = re.compile(r"/actions/runs/(\d+)/job/\d+")
PASSING = {"success", "neutral", "skipped"}  # conclusions that are not failing
QUIET = {"neutral", "skipped"}
MAX_FIELD = 200  # characters of one field GitHub sent
MAX_NAMES = 20  # names in a verdict line


@dataclass(frozen=True)
class Context:
    rules: Rules
    api: GitHub
    git: Git
    max_output: int


@dataclass(frozen=True)
class Check:
    name: str
    running: bool
    failing: bool
    quiet: bool  # neutral or skipped
    line: str


# --- checks --------------------------------------------------------------------------------------


def checks(ctx: Context, repo: str, ref: object) -> str:
    """The check runs and commit statuses of the commit `ref` names, one line each, then a verdict."""
    ctx.rules.access(repo)
    where, sha = _resolve(ctx, repo, _ref(ref))
    runs = _needs(repo, "Checks", lambda: ctx.api.check_runs(repo, sha))
    statuses = _needs(repo, "Commit statuses", lambda: ctx.api.statuses(repo, sha))
    found = [_check_run(r) for r in runs if isinstance(r, dict)] + [_status(s) for s in statuses if isinstance(s, dict)]
    head = [f"Checks of {repo} {where} at {_field(sha)}:"]
    return _fit(head + [c.line for c in found], [_verdict(found)], ctx.max_output)


def _ref(ref: object) -> int | str:
    """A pull request number, or a branch name or commit SHA to look up; refused before GitHub is asked."""
    if type(ref) is int:
        return _number(ref, "ref")
    if not isinstance(ref, str) or not ref:
        raise Refused(f'ref must be a pull request number (12 or "#12"), a branch name or a commit SHA, not {ref!r}.')
    if ref.startswith("#"):
        m = PULL_REF.fullmatch(ref)
        return _number(int(m.group(1)) if m else ref, "ref")
    return ref


def _resolve(ctx: Context, repo: str, ref: int | str) -> tuple[str, str]:
    """(what `ref` named, as words; the commit SHA it names)."""
    api = ctx.api
    if isinstance(ref, int):
        pull = _needs(repo, "Pull requests", lambda: api.pull(repo, ref))
        if pull is None:
            raise Refused(f"{repo} has no pull request #{ref}.")
        return f"pull request #{ref} ({_field(pull['head'].get('ref'))})", pull["head"]["sha"]
    if SHA.fullmatch(ref):
        commit = _needs(repo, "Contents", lambda: api.commit(repo, ref))
        if commit is not None:
            return f"commit {ref}", commit["sha"]
    ctx.git.check_branch(ref, ctx.rules)  # a name check-ref-format refuses can hold "..": never in a URL
    branch = _needs(repo, "Contents", lambda: api.branch(repo, ref))
    if branch is not None:
        return f"branch {ref}", branch["sha"]
    if SHA.fullmatch(ref):
        raise Refused(f"{repo} has no commit or branch {ref}.")
    hint = f' For pull request {ref}, pass {ref} or "#{ref}".' if ref.isdigit() else ""
    raise Refused(f"{repo} has no branch {ref}.{hint}")


def _check_run(run: dict) -> Check:
    status, conclusion = run.get("status"), run.get("conclusion")
    running = status != "completed"
    parts = [_field(status), *([] if running else [_field(conclusion)])]
    if not running and (took := _duration(run.get("started_at"), run.get("completed_at"))):
        parts.append(took)
    m = ACTIONS_JOB.search(run.get("details_url") or "")
    if m:
        parts.append(f"run {m.group(1)}")
    url = run.get("html_url") or run.get("details_url")
    if url:
        parts.append(_field(url))
    name = _field(run.get("name"))
    return Check(
        name,
        running,
        not running and conclusion not in PASSING,
        not running and conclusion in QUIET,
        f"- {name}: {', '.join(parts)}",
    )


def _status(status: dict) -> Check:
    state = status.get("state")
    parts = [_field(state)]
    if status.get("description"):
        parts.append(_field(status["description"]))
    if status.get("target_url"):
        parts.append(_field(status["target_url"]))
    name = _field(status.get("context"))
    return Check(
        name, state == "pending", state not in ("pending", "success"), False,
        f"- {name} (commit status): {', '.join(parts)}",
    )  # fmt: skip


def _verdict(found: list[Check]) -> str:
    if not found:
        return "No checks reported."
    failing = [c.name for c in found if c.failing]
    running = sum(c.running for c in found)
    if failing:
        still = f"; {running} still running" if running else ""
        return f"{len(failing)} failing: {_names(failing)}{still}."
    if running:
        return f"{running} still running."
    quiet = sum(c.quiet for c in found)
    note = f" ({quiet} neutral or skipped, which count as not failing)" if quiet else ""
    return f"All {len(found)} passed{note}."


# --- shared --------------------------------------------------------------------------------------


def _needs(repo: str, permission: str, read: Callable):
    """`read()`, with a 403 or 404 explained as the token lacking `permission` (read) on `repo`.

    Lookups that may find nothing (a pull request, branch or commit) take their 404 as an answer
    before this sees it, so a 404 here is about access.
    """
    try:
        return read()
    except RateLimited:
        raise
    except GitHubError as e:
        if e.status not in (403, 404):
            raise
        raise FromGitHub(
            f'{e} The gate\'s token may lack the "{permission}" repository permission (read) on {repo}; '
            "granting it is the user's, in the token's settings on GitHub."
        ) from None


def _number(value: object, what: str) -> int:
    if type(value) is not int or value <= 0:
        raise Refused(f"{what} must be a positive whole number, not {value!r}.")
    return value


def _field(value: object) -> str:
    """One field GitHub sent, as one printable line of at most MAX_FIELD characters."""
    text = "-" if value is None else str(value)
    text = "".join(c if c.isprintable() else " " for c in text).strip() or "-"
    return text if len(text) <= MAX_FIELD else text[: MAX_FIELD - 3] + "..."


def _names(names: list[str]) -> str:
    more = f" and {len(names) - MAX_NAMES} more" if len(names) > MAX_NAMES else ""
    return ", ".join(names[:MAX_NAMES]) + more


def _duration(start: object, end: object) -> str | None:
    try:
        seconds = int((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds())
    except TypeError, ValueError:
        return None
    if seconds < 0:
        return None
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return " ".join(f"{n}{unit}" for n, unit in ((h, "h"), (m, "m"), (s, "s")) if n) or "0s"


def _fit(body: list[str], end: list[str], limit: int) -> str:
    """The lines, cut in the middle when too long, so the verdict at the end always shows."""
    text, tail = "\n".join(body), "\n".join(end)
    room = limit - len(tail) - 40
    if len(text) > room:
        text = text[:room] + f"\n[... {len(text) - room} characters cut ...]"
    return f"{text}\n{tail}"
