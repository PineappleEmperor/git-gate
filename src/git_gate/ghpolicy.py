"""Which `gh` invocations the gate runs, and how it rewrites them. Pure: no I/O."""

import re
from dataclasses import dataclass, field

from git_gate import Refused

READ = {
    ("pr", "view"), ("pr", "list"), ("pr", "diff"), ("pr", "checks"),
    ("issue", "view"), ("issue", "list"),
    ("run", "list"), ("run", "view"), ("run", "watch"), ("run", "download"),
    ("workflow", "list"), ("workflow", "view"),
    ("release", "list"), ("release", "view"), ("release", "download"),
    ("label", "list"),
    ("repo", "view"),
    ("api",),
}  # fmt: skip
# gh is read-only: each former write has a typed tool with checked parameters instead.
TYPED_WRITES = {
    ("pr", "create"): "use the gate's pr tool, action create",
    ("pr", "edit"): (
        "use the gate's pr tool, action edit, for the title, body and base, and its triage tool for labels, "
        "assignees and reviewers"
    ),
    ("pr", "comment"): "use the gate's pr tool, action comment",
    ("pr", "review"): (
        "use the gate's pr tool, action review_comment or request_changes; approving is the user's, "
        "since the token acts as the user"
    ),
    ("pr", "close"): "use the gate's pr tool, action close",
    ("pr", "reopen"): "use the gate's pr tool, action reopen",
    ("pr", "ready"): "use the gate's pr tool, action ready",
    ("issue", "create"): "use the gate's issue tool, action create",
    ("issue", "edit"): (
        "use the gate's issue tool, action edit, for the title and body, and its triage tool for labels and assignees"
    ),
    ("issue", "comment"): "use the gate's issue tool, action comment",
    ("issue", "close"): "use the gate's issue tool, action close",
    ("issue", "reopen"): "use the gate's issue tool, action reopen",
    ("run", "rerun"): "use the gate's workflow_run tool, action rerun (or rerun_failed for --failed)",
    ("run", "cancel"): "use the gate's workflow_run tool, action cancel",
    ("label", "create"): "use the gate's label tool, action create",
    ("label", "edit"): "use the gate's label tool, action edit",
}
REFUSED_HINTS = {
    ("repo", "clone"): "use the gate's clone tool",
    ("pr", "checkout"): "use the gate's fetch tool, then check out locally",
    ("release", "create"): "releases create tags, which stay manual",
    ("release", "edit"): "releases move tags, which stay manual",
    ("release", "upload"): "release changes stay manual",
    ("issue", "develop"): "it creates a remote branch outside push's rules; push a branch instead",
    ("pr", "merge"): "merging lands code on the base branch, which stays the user's to do",
    ("search", "issues"): "GitHub ORs repo: qualifiers, so it cannot be held to one repo; use issue list --search",
    ("search", "prs"): "GitHub ORs repo: qualifiers, so it cannot be held to one repo; use pr list --search",
    **TYPED_WRITES,
}

DENIED_FLAGS = {"-R", "--repo", "--hostname", "--web", "-w", "--editor", "-e", "--recover", "--admin", "--owner"}
BODY_FILE_FLAGS = {"--body-file", "-F"}
DOWNLOAD_DIR_FLAGS = {"-D", "--dir"}
DOWNLOAD_FILE_FLAGS = {"-O", "--output"}

API_VALUE_FLAGS = {
    "-X", "--method", "-H", "--header", "-f", "--raw-field", "-F", "--field",
    "-q", "--jq", "-t", "--template", "--cache", "-p", "--preview",
}  # fmt: skip
API_BOOL_FLAGS = {"--paginate", "--slurp", "-i", "--include", "--silent", "--verbose"}
API_DENIED = {"--input", "--hostname"}

URL_LIKE = re.compile(r"(?i)^(?:[a-z][a-z0-9+.-]*://|git@)\S*$|^\S*github\.com[/:]\S*$")
SHORT_CLUSTER = re.compile(r"^-[A-Za-z]\S+$")
# `--search` builds a GitHub search query; any of these would widen it past the forced repo.
SCOPE_QUALIFIER = re.compile(r"(?i)\b(?:repo|org|user|owner):|\bis:(?:public|private)\b")
SEARCH_FLAGS = {"--search", "-S"}


@dataclass
class GhPlan:
    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)
    download: tuple[int, str, bool] | None = None  # (argv index, agent path, is_dir)


def plan(repo: str, args: list[str]) -> GhPlan:
    if not args:
        raise Refused("gh needs a subcommand.")
    for a in args:
        if not isinstance(a, str) or "\0" in a:
            raise Refused("gh arguments must be strings without NUL bytes.")
    cmd = (args[0],) if args[0] == "api" else tuple(args[:2])
    if cmd in REFUSED_HINTS:
        raise Refused(f"gh {' '.join(cmd)} is not available through the gate: {REFUSED_HINTS[cmd]}.")
    if cmd not in READ:
        raise Refused(f"gh {' '.join(cmd)} is not on the gate's subcommand allowlist.")
    rest = list(args[len(cmd) :])
    if cmd == ("api",):
        return _plan_api(repo, rest)
    return _plan_command(repo, cmd, rest)


def _flag_name(token: str) -> str:
    return token.split("=", 1)[0] if token.startswith("--") else token


def _common_checks(tokens: list[str]) -> None:
    for t in tokens:
        if t == "--":
            continue
        if SHORT_CLUSTER.match(t) and not t.startswith("--"):
            raise Refused(f"Pass short flags and their values as separate arguments ({t!r}).")
        if URL_LIKE.match(t):
            raise Refused(
                f"{t!r} looks like a URL; the gate fixes the repository, so name PRs, issues and runs "
                "by number. Put URLs in a body file instead of a bare argument."
            )


def _plan_command(repo: str, cmd: tuple[str, ...], rest: list[str]) -> GhPlan:
    _common_checks(rest)
    result = GhPlan(argv=[])
    out: list[str] = []
    i = 0
    while i < len(rest):
        t = rest[i]
        name = _flag_name(t)
        if name in DENIED_FLAGS:
            raise Refused(f"gh flag {name} is not allowed through the gate.")
        if name == "--clobber":
            raise Refused(
                "gh --clobber is not allowed through the gate: downloads go into a new directory "
                "(or a new file) the gate creates, and never replace anything."
            )
        if name in BODY_FILE_FLAGS:
            raise Refused(f"gh {name} is not allowed: gh is read-only, and bodies go through the gate's typed tools.")
        if name in SEARCH_FLAGS:
            value = t.split("=", 1)[1] if "=" in t else (rest[i + 1] if i + 1 < len(rest) else "")
            if SCOPE_QUALIFIER.search(value):
                raise Refused("--search may not name repo:, org:, user:, owner: or is:public/private.")
        if cmd[1] == "download" and name in DOWNLOAD_DIR_FLAGS | DOWNLOAD_FILE_FLAGS:
            if "=" in t and t.startswith("--"):
                value = t.split("=", 1)[1]
                out.append(name)
            else:
                if i + 1 >= len(rest):
                    raise Refused(f"gh flag {name} needs a value.")
                value = rest[i + 1]
                out.append(t)
                i += 1
            out.append(value)
            if result.download:
                raise Refused("Give one download target.")
            result.download = (len(out) - 1, value, name in DOWNLOAD_DIR_FLAGS)
        else:
            out.append(t)
        i += 1

    if cmd[1] == "download" and not result.download:
        raise Refused("gh download needs an absolute -D/--dir (or release download -O) inside a workspace.")
    if result.download and result.download[1] == "-":
        raise Refused("Downloads must go to a file or directory, not stdout.")

    prefix = ["repo", "view", repo] if cmd == ("repo", "view") else [*cmd, "-R", repo]
    offset = len(prefix)
    result.argv = prefix + out
    if result.download:
        idx, path, is_dir = result.download
        result.download = (idx + offset, path, is_dir)
    return result


def _plan_api(repo: str, rest: list[str]) -> GhPlan:
    _common_checks(rest)
    out: list[str] = []
    endpoint = None
    i = 0
    while i < len(rest):
        t = rest[i]
        if t.startswith("-") and t != "-":
            name = _flag_name(t)
            if name in API_DENIED:
                raise Refused(f"gh api {name} is not allowed through the gate.")
            if name in API_BOOL_FLAGS:
                out.append(t)
                i += 1
                continue
            if name not in API_VALUE_FLAGS:
                raise Refused(f"gh api flag {name} is not on the gate's list.")
            if "=" in t and t.startswith("--"):
                value = t.split("=", 1)[1]
            else:
                if i + 1 >= len(rest):
                    raise Refused(f"gh api {name} needs a value.")
                value = rest[i + 1]
                i += 1
            i += 1
            if name in ("-X", "--method"):
                if value.upper() != "GET":
                    raise Refused("gh api is GET-only through the gate.")
                continue
            if name in ("-H", "--header") and not value.lower().startswith("accept:"):
                raise Refused("gh api accepts only Accept: headers through the gate.")
            if name in ("-F", "--field") and value.partition("=")[2].startswith("@"):
                raise Refused("gh api -F key=@file is not allowed: the gate does not read files for api.")
            out += [name, value]
            continue
        if endpoint is not None:
            raise Refused("gh api takes one endpoint.")
        endpoint = t
        out.append(t)
        i += 1
    if endpoint is None:
        raise Refused("gh api needs an endpoint.")
    _check_endpoint(repo, endpoint)
    return GhPlan(argv=["api", "--method", "GET", *out], env={"GH_REPO": repo})


def _check_endpoint(repo: str, endpoint: str) -> None:
    path = endpoint.lstrip("/").split("?", 1)[0].split("#", 1)[0]
    segs = path.split("/")
    if "%" in path or "\\" in path or any(s in ("", ".", "..") for s in segs[:3]) or ".." in segs or "." in segs:
        raise Refused(f"gh api endpoint {endpoint!r} is not a plain path.")
    owner, name = repo.lower().split("/")
    head = [s.lower() for s in segs[:3]]
    if head not in (["repos", "{owner}", "{repo}"], ["repos", owner, name]):
        raise Refused(f"gh api endpoint must be under repos/{{owner}}/{{repo}} ({repo}); got {endpoint!r}.")
