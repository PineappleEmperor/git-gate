"""Rules and token, read from ~/.config/git-gate on every call."""

import os
import re
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from git_gate import Refused

# Identifier patterns are always used with fullmatch: match with "$" accepts a trailing newline.
REPO_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}")
ACCESS_LEVELS = ("read", "write")
REPO_KEYS = ("access", "handover_paths", "push_deploys")
TOP_KEYS = ("workspaces", "protected_branches", "repos", "git_timeout_seconds", "gh_timeout_seconds")
MAX_TIMEOUT = 86400
OFF_SWITCH_MESSAGE = "git-gate is switched off: remove ~/.config/git-gate/disabled to turn it back on."


def canonical_pattern(pattern: str) -> str:
    """The one form a handover pattern is matched in: case-folded, anchored at the repository root.

    Empty and "." segments are dropped (so a leading "/", "./" or "//" is harmless), and so are
    trailing "**" segments, since a pattern covers everything below what it names anyway. What is
    left empty means everything, stored as "**". Anything that could silently match less than it
    reads as is refused: "..", a backslash, "**" glued to other characters, unprintable text.
    """
    if not isinstance(pattern, str) or not pattern:
        raise Refused(f"handover pattern {pattern!r} is empty.")
    if not pattern.isprintable():
        raise Refused(f"handover pattern {pattern!r} has unprintable characters.")
    if "\\" in pattern:
        raise Refused(f"handover pattern {pattern!r} has a backslash; separate directories with /.")
    parts = pattern.casefold().split("/")
    if ".." in parts:
        raise Refused(f"handover pattern {pattern!r} has a '..' segment.")
    if any("**" in part and part != "**" for part in parts):
        raise Refused(f"handover pattern {pattern!r} has '**' inside a segment; use it as a whole segment.")
    parts = [part for part in parts if part not in ("", ".")]
    while parts and parts[-1] == "**":
        parts.pop()
    return "/".join(parts) or "**"


DEFAULT_HANDOVER_PATHS = tuple(
    canonical_pattern(p)
    for p in (
        ".github/workflows/**", ".githooks/**", ".pre-commit-config.yaml", ".claude/**", ".mcp.json",
        ".vscode/**", ".idea/**", ".envrc", ".devcontainer/**",
    )
)  # fmt: skip


def config_dir() -> Path:
    return Path(os.environ.get("GIT_GATE_CONFIG_DIR", Path.home() / ".config/git-gate"))


def state_dir() -> Path:
    return Path(os.environ.get("GIT_GATE_STATE_DIR", Path.home() / ".local/state/git-gate"))


@dataclass(frozen=True)
class RepoRules:
    access: str  # "read" | "write"
    handover_paths: tuple[str, ...]
    push_deploys: bool


@dataclass(frozen=True)
class Rules:
    workspaces: tuple[Path, ...]
    repos: dict[str, RepoRules]  # lower-case "owner/name"
    protected_branches: tuple[str, ...] = ("main", "master")  # fnmatchcase patterns: "*" crosses "/"
    git_timeout: int = 300
    gh_timeout: int = 1800
    forbidden: tuple[Path, ...] = field(default=())

    def repo(self, repo: str) -> RepoRules:
        if not isinstance(repo, str) or not REPO_RE.fullmatch(repo) or repo.endswith(".git") or ".." in repo:
            raise Refused(f"'{repo}' is not an owner/name repository.")
        rules = self.repos.get(repo.lower())
        if rules is None:
            raise Refused(f"{repo} is not on the git-gate repo allowlist (rules.toml [repos]).")
        return rules

    def access(self, repo: str) -> str:
        return self.repo(repo).access

    def require_write(self, repo: str) -> None:
        if self.access(repo) != "write":
            raise Refused(f"{repo} is read-only in rules.toml; this call needs write access.")


def _parse_repo(path: Path, name: str, entry: object) -> RepoRules:
    if isinstance(entry, str):
        entry = {"access": entry}
    if not isinstance(entry, dict):
        raise Refused(f"{path}: bad repo entry {name!r} = {entry!r}.")
    unknown = sorted(set(entry) - set(REPO_KEYS))
    if unknown:
        raise Refused(f"{path}: repo {name!r} has unknown keys {unknown}; allowed: {list(REPO_KEYS)}.")
    if entry.get("access") not in ACCESS_LEVELS:
        raise Refused(f'{path}: repo {name!r} needs access = "read" or "write".')
    extra = entry.get("handover_paths", [])
    if not isinstance(extra, list) or not all(isinstance(p, str) for p in extra):
        raise Refused(f"{path}: repo {name!r} handover_paths must be a list of path patterns.")
    try:
        patterns = [canonical_pattern(p) for p in extra]
    except Refused as e:
        raise Refused(f"{path}: repo {name!r}: {e}") from None
    push_deploys = entry.get("push_deploys", False)
    if not isinstance(push_deploys, bool):
        raise Refused(f"{path}: repo {name!r} push_deploys must be true or false.")
    return RepoRules(entry["access"], tuple(dict.fromkeys(DEFAULT_HANDOVER_PATHS + tuple(patterns))), push_deploys)


def _strings(path: Path, data: dict, key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = data.get(key, list(default))
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise Refused(f"{path}: {key} must be a list of non-empty strings, got {value!r}.")
    return tuple(value)


def _timeout(path: Path, data: dict, key: str, default: int) -> int:
    value = data.get(key, default)
    if type(value) is not int or not 1 <= value <= MAX_TIMEOUT:
        raise Refused(f"{path}: {key} must be a whole number of seconds from 1 to {MAX_TIMEOUT}, got {value!r}.")
    return value


def check_enabled(cfg: Path | None = None) -> None:
    if ((cfg or config_dir()) / "disabled").exists():
        raise Refused(OFF_SWITCH_MESSAGE)


def load_rules(cfg: Path | None = None, extra_forbidden: tuple[Path, ...] = ()) -> Rules:
    cfg = cfg or config_dir()
    path = cfg / "rules.toml"
    try:
        data = tomllib.loads(path.read_text())
    except FileNotFoundError:
        raise Refused(f"No rules file at {path}; the user has to create it.") from None
    except tomllib.TOMLDecodeError as e:
        raise Refused(f"{path} is not valid TOML: {e}") from None

    unknown = sorted(set(data) - set(TOP_KEYS))
    if unknown:
        raise Refused(f"{path}: unknown top-level keys {unknown}; allowed: {list(TOP_KEYS)}.")
    table = data.get("repos", {})
    if not isinstance(table, dict):
        raise Refused(f"{path}: repos must be a table of owner/name entries.")
    repos = {}
    for name, entry in table.items():
        if not REPO_RE.fullmatch(name):
            raise Refused(f"{path}: bad repo entry {name!r} = {entry!r}.")
        if name.lower() in repos:
            raise Refused(f"{path}: repo {name!r} is listed twice (repo names ignore case).")
        repos[name.lower()] = _parse_repo(path, name, entry)

    forbidden = tuple(p.resolve() for p in (cfg, state_dir(), Path("/opt/git-gate"), *extra_forbidden))
    workspaces = []
    for w in _strings(path, data, "workspaces", ()):
        wp = Path(w)
        if not wp.is_absolute():
            raise Refused(f"{path}: workspace {w!r} must be absolute.")
        wp = wp.resolve()
        for f in forbidden:
            if wp.is_relative_to(f) or f.is_relative_to(wp):
                raise Refused(f"{path}: workspace {wp} overlaps gate-owned {f}.")
        workspaces.append(wp)

    return Rules(
        workspaces=tuple(workspaces),
        repos=repos,
        protected_branches=_strings(path, data, "protected_branches", ("main", "master")),
        git_timeout=_timeout(path, data, "git_timeout_seconds", 300),
        gh_timeout=_timeout(path, data, "gh_timeout_seconds", 1800),
        forbidden=forbidden,
    )


def load_token(cfg: Path | None = None) -> str:
    path = (cfg or config_dir()) / "token"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise Refused(f"No token at {path}; the user has to create it.") from None
    except OSError as e:
        raise Refused(f"Cannot open {path}: {e.strerror}.") from None
    with os.fdopen(fd) as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise Refused(f"{path} must be a regular file owned by you with mode 0600.")
        token = f.read().strip()
    if not token or any(c.isspace() for c in token):
        raise Refused(f"{path} must hold the token on a single line.")
    return token
