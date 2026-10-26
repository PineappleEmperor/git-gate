"""gh is retired: which gate tool replaces each gh command an agent used to run. Pure: no I/O.

Agents in a gate session never call gh. Conversations started on a release that still registered
the gh tool can call it until they end; each such call is refused with the tool that does the job.
"""

READS = {
    ("pr", "view"): 'the pr tool, action "view"',
    ("pr", "list"): 'the pr tool, action "list"',
    ("pr", "diff"): 'the pr tool, action "diff"',
    ("pr", "status"): 'the pr tool, action "list"',
    ("pr", "checks"): "the checks tool (with wait=true instead of --watch)",
    ("run", "list"): "the runs tool",
    ("run", "view"): "the run tool (its reply has the end of each failed job's log)",
    ("run", "watch"): "the run tool, with wait=true",
    ("run", "download"): "nothing: downloading run artifacts is not supported; the run tool shows failed jobs' logs",
    ("issue", "view"): 'the issue tool, action "view"',
    ("issue", "list"): 'the issue tool, action "list"',
    ("issue", "status"): 'the issue tool, action "list"',
    ("release", "list"): 'the release tool, action "list"',
    ("release", "view"): 'the release tool, action "view"',
    ("release", "download"): "nothing: downloading release assets is not supported; release view lists them",
    ("repo", "view"): 'the api_get tool, path ""',
    ("workflow", "list"): 'the api_get tool, path "actions/workflows"',
    ("workflow", "view"): 'the api_get tool, path "actions/workflows" (or "actions/workflows/<file>")',
    ("label", "list"): 'the api_get tool, path "labels"',
    ("api",): "the api_get tool (a GET of a path relative to the repo, with query for its parameters)",
}  # fmt: skip
WRITES = {
    ("pr", "create"): "the pr tool, action create",
    ("pr", "edit"): "the pr tool, action edit, and the triage tool for labels, assignees and reviewers",
    ("pr", "comment"): "the pr tool, action comment",
    ("pr", "review"): "the pr tool, action review_comment or request_changes; approving is the user's",
    ("pr", "close"): "the pr tool, action close",
    ("pr", "reopen"): "the pr tool, action reopen",
    ("pr", "ready"): "the pr tool, action ready",
    ("issue", "create"): "the issue tool, action create",
    ("issue", "edit"): "the issue tool, action edit, and the triage tool for labels and assignees",
    ("issue", "comment"): "the issue tool, action comment",
    ("issue", "close"): "the issue tool, action close",
    ("issue", "reopen"): "the issue tool, action reopen",
    ("run", "rerun"): "the workflow_run tool, action rerun (or rerun_failed for --failed)",
    ("run", "cancel"): "the workflow_run tool, action cancel",
    ("label", "create"): "the label tool, action create",
    ("label", "edit"): "the label tool, action edit",
    ("repo", "clone"): "the clone tool",
    ("pr", "checkout"): "the fetch tool, then a local checkout",
    ("pr", "merge"): "nothing: merging lands code on the base branch, which stays the user's",
    ("release", "create"): "nothing: releases create tags, which stay the user's",
    ("release", "edit"): "nothing: releases move tags, which stay the user's",
    ("release", "upload"): "nothing: release changes stay the user's",
}
ALL = "the gate's typed tools: pr, issue, checks, run, runs, release, file, api_get, and the write tools"


def retired(args: object) -> str:
    """The refusal of a gh call: gh is retired, and what to use instead."""
    words = [a for a in args if isinstance(a, str)][:2] if isinstance(args, list | tuple) else []
    command = tuple(words)
    use = READS.get(command) or WRITES.get(command) or READS.get(command[:1]) or ALL
    shown = f"gh {' '.join(words)}".strip() if all(w.isprintable() and len(w) < 40 for w in words) else "gh"
    return (
        f"gh is retired in git-gate: agents never call gh. Instead of `{shown}`, use {use}. A tool added "
        "after this conversation started is not in its tool list: ask the user to start a new conversation."
    )
