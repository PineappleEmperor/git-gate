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
WRITE = {
    ("pr", "create"), ("pr", "edit"), ("pr", "comment"), ("pr", "review"), ("pr", "close"),
    ("pr", "reopen"), ("pr", "ready"),
    ("issue", "create"), ("issue", "edit"), ("issue", "comment"), ("issue", "close"),
    ("issue", "reopen"),
    ("run", "rerun"), ("run", "cancel"),
    ("label", "create"), ("label", "edit"),
}  # fmt: skip
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
    uploads: dict[int, str] = field(default_factory=dict)  # argv index -> agent path to copy
    download: tuple[int, str, bool] | None = None  # (argv index, agent path, is_dir)
    clobber: bool = False


def plan(repo: str, access: str, args: list[str]) -> GhPlan:
    if not args:
        raise Refused("gh needs a subcommand.")
    for a in args:
        if not isinstance(a, str) or "\0" in a:
            raise Refused("gh arguments must be strings without NUL bytes.")
    cmd = (args[0],) if args[0] == "api" else tuple(args[:2])
    if cmd in REFUSED_HINTS:
        raise Refused(f"gh {' '.join(cmd)} is not available through the gate: {REFUSED_HINTS[cmd]}.")
    if cmd not in READ | WRITE:
        raise Refused(f"gh {' '.join(cmd)} is not on the gate's subcommand allowlist.")
    if cmd in WRITE and access != "write":
        raise Refused(f"gh {' '.join(cmd)} needs write access; {repo} is read-only.")
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
            result.clobber = True
        if name in SEARCH_FLAGS:
            value = t.split("=", 1)[1] if "=" in t else (rest[i + 1] if i + 1 < len(rest) else "")
            if SCOPE_QUALIFIER.search(value):
                raise Refused("--search may not name repo:, org:, user:, owner: or is:public/private.")
        wants_path = name in BODY_FILE_FLAGS or (
            cmd[1] == "download" and name in DOWNLOAD_DIR_FLAGS | DOWNLOAD_FILE_FLAGS
        )
        if wants_path:
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
            index = len(out) - 1
            if name in BODY_FILE_FLAGS:
                if value != "-":
                    result.uploads[index] = value
            else:
                if result.download:
                    raise Refused("Give one download target.")
                result.download = (index, value, name in DOWNLOAD_DIR_FLAGS)
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
    result.uploads = {k + offset: v for k, v in result.uploads.items()}
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
