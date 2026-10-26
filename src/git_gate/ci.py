"""Read-only CI status: whether a pull request, branch or commit passed its checks.

Agents learnt this from `gh pr checks` and `gh run watch`, whose requests are gh's own business:
`--watch` polls every few seconds on the shared token. These tools make a known, small set of
REST calls instead. Everything GitHub sends is printed as one printable line per field, and the
gate marks the whole reply as GitHub content.
"""

import re
import time
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
LOG_STAMP = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?Z ")  # GitHub's prefix on each log line
STEP_ERROR = "##[error]"  # the line GitHub writes when a step fails
PASSING = {"success", "neutral", "skipped"}  # conclusions that are not failing
QUIET = {"neutral", "skipped"}
MAX_FIELD = 200  # characters of one field GitHub sent
MAX_NAMES = 20  # names in a verdict line
FIRST_INTERVAL = 15.0  # seconds between polls while waiting, doubling up to MAX_INTERVAL
MAX_INTERVAL = 60.0
FIRST_BACKOFF = 60.0  # seconds to back off from a rate limit that names no wait, doubling up to MAX_BACKOFF
MAX_BACKOFF = 300.0
# Claude Code aborts a stdio tool call that sends nothing for 30 minutes; a wait stays well inside that.
MAX_WAIT_MINUTES = 20
LOG_LINES = 80  # lines of a failed job's log shown, from its end
MAX_LOGS = 10  # failed jobs whose logs are read
MAX_LOG_LINE = 1000  # characters of one log line
# ANSI escape sequences: CSI (colours, cursor), OSC (titles, links) ended by BEL or ST, and two-byte escapes.
ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[@-Z\\-_])")


@dataclass(frozen=True)
class Context:
    rules: Rules
    api: GitHub
    git: Git
    max_output: int
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic  # for the wait's deadline
    clock: Callable[[], float] = time.time  # for GitHub's x-ratelimit-reset, a Unix time
    report: Callable[[int, float], None] = lambda polls, waited: None  # for audit.log, once per call
    # Raises Refused when the call would now be refused: run before every poll that follows a sleep.
    recheck: Callable[[], None] = lambda: None


@dataclass(frozen=True)
class Check:
    name: str
    running: bool
    failing: bool
    quiet: bool  # neutral or skipped
    line: str


# --- checks --------------------------------------------------------------------------------------


def checks(ctx: Context, repo: str, ref: object, wait: object = False, timeout_minutes: object = 20) -> str:
    """The check runs and commit statuses of the commit `ref` names, one line each, then a verdict.

    `ref` is resolved to a commit once; with `wait`, its checks are polled until none is queued or
    in progress, or the timeout passes.
    """
    ctx.rules.access(repo)
    ref = _ref(ref)
    minutes, capped = _wait_args(wait, timeout_minutes)
    resolved: list[tuple[str, str]] = []

    def poll() -> list[Check]:
        if not resolved:
            resolved.append(_resolve(ctx, repo, ref))
        sha = resolved[0][1]
        runs = needs(repo, "Checks", lambda: ctx.api.check_runs(repo, sha))
        statuses = needs(repo, "Commit statuses", lambda: ctx.api.statuses(repo, sha))
        return [_check_run(r) for r in runs if isinstance(r, dict)] + [
            _status(s) for s in statuses if isinstance(s, dict)
        ]

    found, note = _poll(ctx, poll, lambda found: any(c.running for c in found), wait, minutes, capped)
    where, sha = resolved[0]
    head = [f"Checks of {repo} {where} at {field(sha)}:"]
    return fit(head + [c.line for c in found], [_verdict(found), *note], ctx.max_output)


def _ref(ref: object) -> int | str:
    """A pull request number, or a branch name or commit SHA to look up; refused before GitHub is asked."""
    if type(ref) is int:
        return number(ref, "ref")
    if not isinstance(ref, str) or not ref:
        raise Refused(f'ref must be a pull request number (12 or "#12"), a branch name or a commit SHA, not {ref!r}.')
    if ref.startswith("#"):
        m = PULL_REF.fullmatch(ref)
        return number(int(m.group(1)) if m else ref, "ref")
    return ref


def _resolve(ctx: Context, repo: str, ref: int | str) -> tuple[str, str]:
    """(what `ref` named, as words; the commit SHA it names)."""
    api = ctx.api
    if isinstance(ref, int):
        pull = needs(repo, "Pull requests", lambda: api.pull(repo, ref))
        if pull is None:
            raise Refused(f"{repo} has no pull request #{ref}.")
        return f"pull request #{ref} ({field(pull['head'].get('ref'))})", pull["head"]["sha"]
    if SHA.fullmatch(ref):
        commit = needs(repo, "Contents", lambda: api.commit(repo, ref))
        if commit is not None:
            return f"commit {field(ref)}", commit["sha"]
    ctx.git.check_branch(ref, ctx.rules)  # a name check-ref-format refuses can hold "..": never in a URL
    branch = needs(repo, "Contents", lambda: api.branch(repo, ref))
    if branch is not None:
        return f"branch {field(ref)}", branch["sha"]
    if SHA.fullmatch(ref):
        raise Refused(f"{repo} has no commit or branch {ref}.")
    hint = f' For pull request {ref}, pass {ref} or "#{ref}".' if ref.isdigit() else ""
    raise Refused(f"{repo} has no branch {ref}.{hint}")


def _check_run(run: dict) -> Check:
    status, conclusion = run.get("status"), run.get("conclusion")
    running = status != "completed"
    parts = [field(status), *([] if running else [field(conclusion)])]
    if not running and (took := _duration(run.get("started_at"), run.get("completed_at"))):
        parts.append(took)
    m = ACTIONS_JOB.search(run.get("details_url") or "")
    if m:
        parts.append(f"run {m.group(1)}")
    url = run.get("html_url") or run.get("details_url")
    if url:
        parts.append(field(url))
    name = field(run.get("name"))
    return Check(
        name,
        running,
        not running and conclusion not in PASSING,
        not running and conclusion in QUIET,
        f"- {name}: {', '.join(parts)}",
    )


def _status(status: dict) -> Check:
    state = status.get("state")
    parts = [field(state)]
    if status.get("description"):
        parts.append(field(status["description"]))
    if status.get("target_url"):
        parts.append(field(status["target_url"]))
    name = field(status.get("context"))
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
        return f"{len(failing)} failing: {listing(failing)}{still}."
    if running:
        return f"{running} still running."
    quiet = sum(c.quiet for c in found)
    note = f" ({quiet} neutral or skipped, which count as not failing)" if quiet else ""
    return f"All {len(found)} passed{note}."


# --- run -----------------------------------------------------------------------------------------


def run(ctx: Context, repo: str, run_id: object, wait: object = False, timeout_minutes: object = 20) -> str:
    """A workflow run, its jobs, and for a failed run the end of each failed job's log, then a verdict.

    With `wait`, the run and its jobs are polled until the run completes or the timeout passes;
    the logs are read once, at the end.
    """
    ctx.rules.access(repo)
    n = number(run_id, "run_id")
    minutes, capped = _wait_args(wait, timeout_minutes)
    api = ctx.api

    def poll() -> tuple[dict, list[dict]]:
        found = needs(repo, "Actions", lambda: api.find_run(repo, n))
        if found is None:
            raise Refused(f"{repo} has no workflow run {n}.")
        return found, [j for j in needs(repo, "Actions", lambda: api.jobs(repo, n)) if isinstance(j, dict)]

    (found, jobs), note = _poll(ctx, poll, lambda state: state[0].get("status") != "completed", wait, minutes, capped)
    status, conclusion = found.get("status"), found.get("conclusion")
    done = status == "completed"
    state = ", ".join([field(status), *([field(conclusion)] if done else [])])
    lines = [
        f"Run {n} of {repo}: {field(found.get('name'))} ({field(found.get('event'))} on "
        f"{field(found.get('head_branch'))}, commit {field(found.get('head_sha'))}): {state}",
        field(found.get("html_url")),
        "Jobs:" if jobs else "No jobs reported.",
        *(_job_line(j) for j in jobs),
    ]
    failing = [j for j in jobs if _job_failed(j)]
    if done and conclusion not in PASSING:
        for j in failing[:MAX_LOGS]:
            lines += _log(ctx, repo, j)
        if len(failing) > MAX_LOGS:
            lines.append(f"Logs of {len(failing) - MAX_LOGS} more failed jobs left out: "
                         f"{listing([field(j.get('name')) for j in failing[MAX_LOGS:]])}.")  # fmt: skip
    return fit(lines, [_run_verdict(status, conclusion, jobs, failing), *note], ctx.max_output)


def _job_failed(job: dict) -> bool:
    return job.get("status") == "completed" and job.get("conclusion") not in PASSING


def _job_line(job: dict) -> str:
    status, conclusion = job.get("status"), job.get("conclusion")
    parts = [field(status)]
    if status == "completed":
        parts.append(field(conclusion))
        if took := _duration(job.get("started_at"), job.get("completed_at")):
            parts.append(took)
    line = f"- {field(job.get('name'))}: {', '.join(parts)}"
    if _job_failed(job):
        steps = [s for s in job.get("steps") or [] if isinstance(s, dict)]
        bad = [field(s.get("name")) for s in steps if s.get("conclusion") not in (*PASSING, None)]
        if bad:
            line += f"; failed steps: {listing(bad)}"
    return line


def _log(ctx: Context, repo: str, job: dict) -> list[str]:
    """The last LOG_LINES lines of a failed job's failed step, or of its whole log when the step's
    part cannot be found, or why there are none."""
    name = field(job.get("name"))
    try:
        data, note = needs(repo, "Actions", lambda: ctx.api.job_log(repo, number(job.get("id"), "job id")))
    except Refused as e:
        return [f"The log of {name} is not available: {e}"]
    lines = text(data).split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    step, part = _failed_step(job, lines)
    tail = [line if len(line) <= MAX_LOG_LINE else line[:MAX_LOG_LINE] + "..." for line in part[-LOG_LINES:]]
    where = f'the failed step "{step}" of {name}' if step else f"the log of {name}"
    return [f"Last {len(tail)} lines of {where}" + (f" ({note})" if note else "") + ":", *tail]


def _failed_step(job: dict, lines: list[str]) -> tuple[str | None, list[str]]:
    """The first failed step's name and its part of the job's log, ending at its last error line.

    Each log line starts with GitHub's timestamp and each step reports its start and end, to the
    second; a line without a timestamp continues the one before. This leaves out the steps around
    the failure, the job's cleanup above all. When the step or the timestamps are missing, or no
    line falls in the step's time, the answer is (None, the whole log).
    """
    steps = [s for s in job.get("steps") or [] if isinstance(s, dict)]
    step = next((s for s in steps if s.get("conclusion") not in (*PASSING, None)), None)
    start, end = (step or {}).get("started_at"), (step or {}).get("completed_at")
    if not (isinstance(start, str) and isinstance(end, str) and LOG_STAMP.fullmatch(start[:-1] + "Z ")):
        return None, lines
    start, end = start[:19], end[:19]
    part, stamp = [], None
    for line in lines:
        if found := LOG_STAMP.match(line):
            stamp = found.group(1)
        if stamp is not None and start <= stamp <= end:
            part.append(line)
    if not part:
        return None, lines
    last_error = max((i for i, line in enumerate(part) if STEP_ERROR in line), default=None)
    return field(step.get("name")), part if last_error is None else part[: last_error + 1]


def _run_verdict(status: object, conclusion: object, jobs: list[dict], failing: list[dict]) -> str:
    if status != "completed":
        waiting = sum(j.get("status") != "completed" for j in jobs)
        return f"Still running ({field(status)}): {waiting} of {len(jobs)} jobs not finished."
    if conclusion in QUIET:
        return f"Finished {conclusion}, which counts as not failing."
    if conclusion == "success":
        quiet = sum(j.get("conclusion") in QUIET for j in jobs)
        note = f" ({quiet} neutral or skipped, which count as not failing)" if quiet else ""
        return f"Passed: {len(jobs)} jobs, none failing{note}."
    if failing:
        failed = listing([field(j.get("name")) for j in failing])
        return f"Failed ({field(conclusion)}): {len(failing)} of {len(jobs)} jobs failed: {failed}."
    return f"Failed ({field(conclusion)}): no job failed; see the run's page."


# --- shared --------------------------------------------------------------------------------------


def _wait_args(wait: object, timeout_minutes: object) -> tuple[int, bool]:
    """(minutes to wait at most, whether the agent asked for more than MAX_WAIT_MINUTES)."""
    if type(wait) is not bool:
        raise Refused(f"wait must be true or false, not {wait!r}.")
    minutes = number(timeout_minutes, "timeout_minutes")
    return min(minutes, MAX_WAIT_MINUTES), minutes > MAX_WAIT_MINUTES


def _poll[T](
    ctx: Context, fetch: Callable[[], T], running: Callable[[T], bool], wait: bool, minutes: int, capped: bool
) -> tuple[T, list[str]]:
    """`fetch()` once, or with `wait` until `running` says it is done or `minutes` pass.

    The interval starts at FIRST_INTERVAL seconds and doubles up to MAX_INTERVAL; an answer
    saying the quota is spent puts the next poll after its reset. A rate limit while waiting is
    waited out rather than failing: for Retry-After, else until the quota's reset, when that is a
    finite time ahead (and never less than FIRST_INTERVAL), else for a back-off of FIRST_BACKOFF
    seconds that doubles with every rate limit in a row, up to MAX_BACKOFF. So no sleep is shorter
    than FIRST_INTERVAL. Any other error fails. No sleep runs past the deadline, and with less
    than FIRST_INTERVAL left the waiting stops: the state so far comes back with a line saying so.
    Before each poll after a sleep, `ctx.recheck()` refuses if the call would now be refused.
    """
    deadline = ctx.monotonic() + minutes * 60
    interval, backoff = FIRST_INTERVAL, FIRST_BACKOFF
    state: list[T] = []  # the last state fetched, once there is one
    limited: RateLimited | None = None
    polls, waited = 0, 0.0
    try:
        while True:
            if polls:
                ctx.recheck()  # the off switch, the rules and the token may have changed while it slept
            polls += 1
            try:
                state[:] = [fetch()]
                limited, backoff = None, FIRST_BACKOFF
                if not wait or not running(state[0]):
                    return state[0], []
                delay = max(interval, ctx.api.rate_wait(ctx.clock()))
                interval = min(interval * 2, MAX_INTERVAL)
            except RateLimited as e:
                if not wait:
                    raise
                limited = e
                asked = e.wait(ctx.clock())
                # Only a wait GitHub names and that lies ahead counts; zero or a reset already past would poll at once.
                delay = max(asked, FIRST_INTERVAL) if asked is not None and asked > 0 else backoff
                backoff = min(backoff * 2, MAX_BACKOFF)
            left = deadline - ctx.monotonic()
            if left < FIRST_INTERVAL:  # a shorter sleep would poll sooner than any other
                break
            ctx.sleep(min(delay, left))
            waited += min(delay, left)
    finally:
        ctx.report(polls, waited)
    span = _minutes(minutes) + (f" (timeout_minutes is capped at {MAX_WAIT_MINUTES})" if capped else "")
    if not state:
        raise FromGitHub(f"GitHub rate-limited every request for {span}, so the gate has no answer: {limited}")
    note = f"Still running after {span}; the gate stopped waiting. Call again to keep waiting."
    if limited:
        note += " GitHub rate-limited the last poll, so this is the state before it."
    return state[0], [note]


def _minutes(n: int) -> str:
    return f"{n} minute" if n == 1 else f"{n} minutes"


def needs(repo: str, permission: str, read: Callable):
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


def number(value: object, what: str) -> int:
    if type(value) is not int or value <= 0:
        raise Refused(f"{what} must be a positive whole number, not {value!r}.")
    return value


def field(value: object) -> str:
    """One field GitHub sent, as one printable line of at most MAX_FIELD characters."""
    text = "-" if value is None else str(value)
    text = "".join(c if c.isprintable() else " " for c in text).strip() or "-"
    return text if len(text) <= MAX_FIELD else text[: MAX_FIELD - 3] + "..."


def text(value: bytes | str) -> str:
    """Multi-line text GitHub sent (a log, a body, a diff, a file) as printable lines: bytes decoded
    losslessly, a leading byte-order mark and ANSI escapes removed, line ends made "\\n", and
    anything else unprintable but a tab shown as "?" (bytes that are not UTF-8 among them)."""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "surrogateescape")
    value = ANSI.sub("", value).removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    return "".join(c if c.isprintable() or c in "\n\t" else "?" for c in value)


def listing(items: list[str]) -> str:
    """Names joined by commas, at most MAX_NAMES of them and a count of the rest."""
    more = f" and {len(items) - MAX_NAMES} more" if len(items) > MAX_NAMES else ""
    return ", ".join(items[:MAX_NAMES]) + more


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


def fit(body: list[str], end: list[str], limit: int) -> str:
    """The lines, cut in the middle when too long, so the `end` lines (a verdict) always show."""
    text, tail = "\n".join(body), "\n".join(end)
    room = limit - len(tail) - 40
    if len(text) > room:
        text = text[:room] + f"\n[... {len(text) - room} characters cut ...]"
    return f"{text}\n{tail}" if end else text
