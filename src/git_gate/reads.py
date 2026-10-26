"""Typed GitHub reads: what agents used `gh`'s read commands for, as REST calls the gate makes itself.

Each read needs read access to the repo, checks its arguments before GitHub is asked, and prints
what GitHub sends through ci's cleaners: one printable line per field, and multi-line text with
its escapes removed. The gate marks every reply as GitHub content.
"""

import re

from git_gate import Refused
from git_gate.ci import Context, field, fit, listing, needs, number, text
from git_gate.writes import _action, _given, _show

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
