"""Typed GitHub reads: what agents used `gh`'s read commands for, as REST calls the gate makes itself.

Each read needs read access to the repo, checks its arguments before GitHub is asked, and prints
what GitHub sends through ci's cleaners: one printable line per field, and multi-line text with
its escapes removed. The gate marks every reply as GitHub content.
"""

import re

from git_gate import Refused
from git_gate.ci import Context, field, fit, listing, needs, number, text
from git_gate.writes import MAX_LABEL, _action, _given, _names, _show

# action -> (required, optional, at least one of), as in writes
PR_READS = {
    "view": ({"number"}, set(), set()),
    "list": (set(), {"state", "head", "base", "limit"}, set()),
    "diff": ({"number"}, set(), set()),
}
PR_STATES = ("open", "closed", "merged", "all")
LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
MAX_LIST = 100
DEFAULT_LIST = 30
MERGED_PAGES = 5  # pages of closed pull requests searched for merged ones
ISSUE_READS = {
    "view": ({"number"}, set(), set()),
    "list": (set(), {"state", "labels", "limit"}, set()),
}
ISSUE_STATES = ("open", "closed", "all")
COMMENTS = 20  # the latest comments an issue view shows
ISSUE_PAGES = 3  # pages of 100 issues read to fill a list, pull requests left out
RUN_STATUSES = (
    "completed", "action_required", "cancelled", "failure", "neutral", "skipped", "stale", "success", "timed_out",
    "in_progress", "queued", "requested", "waiting", "pending",
)  # fmt: skip
WORKFLOW = re.compile(r"\d+|[A-Za-z0-9_.-]+\.ya?ml")  # an id, or a file name under .github/workflows
MAX_RUNS = 50
RELEASE_ACTIONS = {
    "list": (set(), {"limit"}, set()),
    "view": (set(), {"tag"}, set()),
}
MAX_RELEASES = 30
REVIEW_STATES = {
    "APPROVED": "approved",
    "CHANGES_REQUESTED": "requested changes",
    "COMMENTED": "commented",
    "DISMISSED": "dismissed",
}


# --- pr --------------------------------------------------------------------------------------------


def pr(ctx: Context, repo: str, action: str, **kwargs) -> str:
    """The read actions of the pr tool; its write actions are in writes."""
    ctx.rules.access(repo)
    given = _given(**kwargs)
    _action("pr", action, PR_READS, given)
    if action == "list":
        return _pr_list(ctx, repo, **{k: given.get(k) for k in ("state", "head", "base", "limit")})
    n = number(kwargs["number"], "number")
    if action == "view":
        return _pr_view(ctx, repo, n)
    return _pr_diff(ctx, repo, n)


def _pr_view(ctx: Context, repo: str, n: int) -> str:
    found = needs(repo, "Pull requests", lambda: ctx.api.pull(repo, n))
    if found is None:
        raise Refused(f"{repo} has no pull request #{n}.")
    reviews = needs(repo, "Pull requests", lambda: ctx.api.reviews(repo, n))
    latest: dict[str, str] = {}  # reviewer -> their latest review, in the order they first reviewed
    for r in reviews if isinstance(reviews, list) else []:
        if isinstance(r, dict) and isinstance(r.get("user"), dict):
            latest[field(r["user"].get("login"))] = REVIEW_STATES.get(r.get("state"), field(r.get("state")).lower())
    requested = [field(u.get("login")) for u in found.get("requested_reviewers") or [] if isinstance(u, dict)]
    requested += [f"{field(t.get('slug'))} (team)" for t in found.get("requested_teams") or [] if isinstance(t, dict)]
    head, base = found.get("head") or {}, found.get("base") or {}
    mergeable = field(found.get("mergeable_state"))
    if found.get("mergeable") is False:
        mergeable += " (has conflicts)"
    lines = [
        f"Pull request #{n} of {repo}: {field(found.get('title'))}",
        f"State: {_pr_state(found)}",
        f"Author: {field((found.get('user') or {}).get('login'))}",
        f"Branches: {field(head.get('ref'))} -> {field(base.get('ref'))}",
        f"Mergeable: {mergeable}",
        f"Labels: {_labels(found) or '-'}",
        f"Requested reviewers: {listing(requested) or '-'}",
        f"Reviews: {listing([f'{who} {what}' for who, what in latest.items()]) or '-'}",
        field(found.get("html_url")),
        "Body:",
        text(found.get("body") or "(empty)"),
    ]
    return fit(lines, [], ctx.max_output)


def _pr_list(ctx: Context, repo: str, state=None, head=None, base=None, limit=None) -> str:
    state = _choice(state or "open", "state", PR_STATES)
    limit = _limit(limit, DEFAULT_LIST, MAX_LIST)
    if head is not None:
        head = _head(ctx, repo, head)
    if base is not None:
        _branch(ctx, base, "base")
    api = ctx.api

    def page(state: str, per_page: int, page: int | None = None) -> list[dict]:
        got = needs(repo, "Pull requests", lambda: api.pulls(repo, state, per_page, page=page, head=head, base=base))
        return [p for p in got if isinstance(p, dict)] if isinstance(got, list) else []

    if state == "merged":
        found = []
        for n in range(1, MERGED_PAGES + 1):
            got = page("closed", MAX_LIST, n)
            found += [p for p in got if p.get("merged_at")]
            if len(found) >= limit or len(got) < MAX_LIST:
                break
        found = found[:limit]
    else:
        found = page(state, limit)
    title = f"Pull requests of {repo} ({state}, most recently updated first):"
    if not found:
        return f"No pull requests of {repo} match (state {state})."
    lines = [
        f"#{field(p.get('number'))} {field(p.get('title'))} [{_pr_state(p)}] "
        f"{field((p.get('head') or {}).get('ref'))} -> {field((p.get('base') or {}).get('ref'))}, "
        f"updated {field(p.get('updated_at'))}"
        for p in found
    ]
    return fit([title, *lines], [], ctx.max_output)


def _pr_diff(ctx: Context, repo: str, n: int) -> str:
    data = needs(repo, "Pull requests", lambda: ctx.api.pull_diff(repo, n, ctx.max_output))
    if data is None:
        raise Refused(f"{repo} has no pull request #{n}.")
    shown = ctx.max_output - 200
    if len(data) > shown:
        cut = len(data) - shown
        return f"{text(data[:shown])}\n[... {cut} or more characters cut: the diff is longer than the gate shows.]"
    return text(data) or f"Pull request #{n} of {repo} changes nothing."


def _pr_state(p: dict) -> str:
    if p.get("merged_at"):
        return "merged"
    return field(p.get("state")) + (", draft" if p.get("draft") else "")


# --- issue -----------------------------------------------------------------------------------------


def issue(ctx: Context, repo: str, action: str, **kwargs) -> str:
    """The read actions of the issue tool; its write actions are in writes."""
    ctx.rules.access(repo)
    given = _given(**kwargs)
    _action("issue", action, ISSUE_READS, given)
    if action == "list":
        return _issue_list(ctx, repo, **{k: given.get(k) for k in ("state", "labels", "limit")})
    return _issue_view(ctx, repo, number(kwargs["number"], "number"))


def _issue_view(ctx: Context, repo: str, n: int) -> str:
    api = ctx.api
    found = needs(repo, "Issues", lambda: api.issue(repo, n))
    if found is None:
        raise Refused(f"{repo} has no issue #{n}.")
    count = found.get("comments") if type(found.get("comments")) is int else 0
    comments: list[dict] = []
    page = max(1, -(-count // 100))  # the last page
    while page >= 1 and len(comments) < COMMENTS and count:
        got = needs(repo, "Issues", lambda page=page: api.issue_comments(repo, n, page))
        comments = [c for c in got if isinstance(c, dict)] + comments if isinstance(got, list) else comments
        page -= 1
    comments = comments[-COMMENTS:]
    assignees = [field(a.get("login")) for a in found.get("assignees") or [] if isinstance(a, dict)]
    state = field(found.get("state"))
    if found.get("state_reason") and found.get("state") == "closed":
        state += f" ({field(found['state_reason'])})"
    lines = [
        f"Issue #{n} of {repo}: {field(found.get('title'))}",
        f"State: {state}"
        + (" (a pull request: pr view shows its branches and reviews)" if found.get("pull_request") else ""),
        f"Author: {field((found.get('user') or {}).get('login'))}",
        f"Labels: {_labels(found) or '-'}",
        f"Assignees: {listing(assignees) or '-'}",
        f"Opened: {field(found.get('created_at'))}, updated {field(found.get('updated_at'))}",
        field(found.get("html_url")),
        "Body:",
        text(found.get("body") or "(empty)"),
    ]
    if count:
        shown = f"last {len(comments)} of {count}" if len(comments) < count else f"{count} of {count}"
        lines.append(f"Comments ({shown}):")
        for c in comments:
            who = field((c.get("user") or {}).get("login"))
            lines += [f"{who} at {field(c.get('created_at'))}:", text(c.get("body") or "(empty)")]
    return fit(lines, [], ctx.max_output)


def _issue_list(ctx: Context, repo: str, state=None, labels=None, limit=None) -> str:
    state = _choice(state or "open", "state", ISSUE_STATES)
    labels = _names(labels or [], "labels", MAX_LABEL)
    if any("," in name for name in labels):
        raise Refused("labels: GitHub takes a comma-separated list, so a label name with a comma cannot be listed.")
    limit = _limit(limit, DEFAULT_LIST, MAX_LIST)
    found: list[dict] = []
    for page in range(1, ISSUE_PAGES + 1):
        got = needs(repo, "Issues", lambda page=page: ctx.api.issues(repo, state, page, labels))
        got = [i for i in got if isinstance(i, dict)] if isinstance(got, list) else []
        found += [i for i in got if "pull_request" not in i]
        if len(found) >= limit or len(got) < MAX_LIST:
            break
    found = found[:limit]
    if not found:
        return f"No issues of {repo} match (state {state}{', labels ' + ', '.join(labels) if labels else ''})."
    lines = [
        f"#{field(i.get('number'))} {field(i.get('title'))} [{field(i.get('state'))}]"
        + (f" labels: {_labels(i)}" if _labels(i) else "")
        + f"; updated {field(i.get('updated_at'))}"
        for i in found
    ]
    return fit([f"Issues of {repo} ({state}, most recently updated first):", *lines], [], ctx.max_output)


# --- runs ------------------------------------------------------------------------------------------


def runs(
    ctx: Context, repo: str, branch: object = None, workflow: object = None, status: object = None, limit: object = 10
) -> str:
    """The newest workflow runs, one line each, filtered by branch, workflow and status."""
    ctx.rules.access(repo)
    if branch is not None:
        _branch(ctx, branch, "branch")
    if workflow is not None and not (isinstance(workflow, str) and WORKFLOW.fullmatch(workflow) and workflow != ".yml"):
        raise Refused(f"workflow must be a workflow id or file name such as ci.yml, not {_show(workflow)}.")
    if status is not None:
        _choice(status, "status", RUN_STATUSES)
    n = _limit(limit, 10, MAX_RUNS)
    got = needs(repo, "Actions", lambda: ctx.api.runs(repo, n, workflow=workflow, branch=branch, status=status))
    if got is None:
        raise Refused(f"{repo} has no workflow {workflow}.")
    found = [r for r in got.get("workflow_runs") or [] if isinstance(r, dict)] if isinstance(got, dict) else []
    if not found:
        return f"No workflow runs of {repo} match."
    lines = []
    for r in found:
        state = [field(r.get("status"))] + ([field(r.get("conclusion"))] if r.get("status") == "completed" else [])
        what = f"{field(r.get('name'))}, {field(r.get('event'))} on {field(r.get('head_branch'))}"
        lines.append(
            f"- {field(r.get('id'))}: {what}, {', '.join(state)}, created {field(r.get('created_at'))}, "
            f"{field(r.get('html_url'))}"
        )
    return fit([f"Workflow runs of {repo}, newest first:", *lines], [], ctx.max_output)


# --- release ---------------------------------------------------------------------------------------


def release(ctx: Context, repo: str, action: object, tag: object = None, limit: object = None) -> str:
    """List releases, or show one: by tag, or the latest published one."""
    ctx.rules.access(repo)
    if action == "download":
        raise Refused(
            "release download is not supported: the gate does not fetch release assets. view lists each asset's "
            "name and size; ask the user for a file you need."
        )
    _action("release", action, RELEASE_ACTIONS, _given(tag=tag, limit=limit))
    if action == "list":
        n = _limit(limit, 10, MAX_RELEASES)
        found = needs(repo, "Contents", lambda: ctx.api.releases(repo, n))
        found = [r for r in found if isinstance(r, dict)] if isinstance(found, list) else []
        if not found:
            return f"{repo} has no releases."
        lines = [f"Releases of {repo}, newest first:"]
        for r in found:
            flags = [f for f in ("draft", "prerelease") if r.get(f)]
            when = f"published {field(r['published_at'])}" if r.get("published_at") else "not published"
            lines.append(
                f"- {field(r.get('tag_name'))}: {field(r.get('name'))}" + (f" [{', '.join(flags)}]" if flags else "")
                + f", {when}, {field(r.get('html_url'))}"
            )  # fmt: skip
        return fit(lines, [], ctx.max_output)
    latest = tag is None or tag == "latest"
    if not latest and not (isinstance(tag, str) and tag and tag.isprintable() and len(tag) <= 255):
        raise Refused(f'tag must be a tag name or "latest", not {_show(tag)}.')
    found = needs(repo, "Contents", lambda: ctx.api.release(repo, None if latest else tag))
    if found is None:
        raise Refused(f"{repo} has no published release." if latest else f"{repo} has no release with tag {tag}.")
    flags = [f for f in ("draft", "prerelease") if found.get(f)]
    assets = [
        f"{field(a.get('name'))} ({_size(a.get('size'))})" for a in found.get("assets") or [] if isinstance(a, dict)
    ]
    lines = [
        f"Release {field(found.get('name'))} of {repo} (tag {field(found.get('tag_name'))})"
        + (f" [{', '.join(flags)}]" if flags else ""),
        f"Published: {field(found.get('published_at'))} by {field((found.get('author') or {}).get('login'))}",
        f"Assets: {listing(assets) or '-'}",
        field(found.get("html_url")),
        "Notes:",
        text(found.get("body") or "(empty)"),
    ]
    return fit(lines, [], ctx.max_output)


def _size(value: object) -> str:
    if type(value) is not int or value < 0:
        return "size unknown"
    if value < 1024:
        return f"{value} B"
    for unit in ("KiB", "MiB", "GiB"):
        value /= 1024
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}"
    return ""


# --- shared ----------------------------------------------------------------------------------------


def _labels(item: dict) -> str:
    return listing([field(label.get("name")) for label in item.get("labels") or [] if isinstance(label, dict)])


def _choice(value: object, what: str, choices: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in choices:
        raise Refused(f"{what} must be one of {', '.join(choices)}, not {_show(value)}.")
    return value


def _limit(value: object, default: int, most: int) -> int:
    if value is None:
        return default
    n = number(value, "limit")
    if n > most:
        raise Refused(f"limit is at most {most}, not {n}.")
    return n


def _branch(ctx: Context, value: object, what: str) -> str:
    if not isinstance(value, str):
        raise Refused(f"{what} must be a branch name, not {_show(value)}.")
    try:
        ctx.git.check_branch(value, ctx.rules)
    except Refused as e:
        raise Refused(f"{what}: {e}") from None
    return value


def _head(ctx: Context, repo: str, value: object) -> str:
    """A head filter as GitHub takes it, "owner:branch"; a bare branch is the repo owner's."""
    if not isinstance(value, str) or value.count(":") > 1:
        raise Refused(f'head must be a branch name or "owner:branch", not {_show(value)}.')
    owner, _, branch = value.rpartition(":")
    owner = owner or repo.split("/")[0]
    if not LOGIN.fullmatch(owner):
        raise Refused(f"head names an owner that is no GitHub login: {_show(owner)}.")
    return f"{owner}:{_branch(ctx, branch, 'head')}"
