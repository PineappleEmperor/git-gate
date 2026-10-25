"""Rules and token, read from ~/.config/git-gate on every call.

A rules file with a [sessions] table is v2: a session is a project folder, and a call from a
conversation running there reaches that session's own repo and the repos the user grants it, and
exchanges files only inside that folder. A file without [sessions] is v1, where every conversation
reaches every repo in [repos] and every workspace.
"""

import os
import re
import stat
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

from git_gate import Refused

# Identifier patterns are always used with fullmatch: match with "$" accepts a trailing newline.
REPO_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}")
GROUP_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
ACCESS_LEVELS = ("read", "write")
REPO_KEYS = ("access", "handover_paths", "push_deploys")
OPTION_KEYS = ("handover_paths", "push_deploys")
SESSION_KEYS = ("repo", "read", "write")
SETTINGS = ("protected_branches", "git_timeout_seconds", "gh_timeout_seconds")
TOP_KEYS = ("workspaces", "protected_branches", "repos", "git_timeout_seconds", "gh_timeout_seconds")
TOP_KEYS_V2 = (*SETTINGS, "groups", "sessions", "repos")
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


def valid_repo(name: object) -> bool:
    return isinstance(name, str) and bool(REPO_RE.fullmatch(name)) and not name.endswith(".git") and ".." not in name


def toml_string(text: str) -> str:
    """`text` as a TOML basic string: quotes and backslashes escaped, and whatever is not printable
    (control characters, bidi and zero-width marks) as \\uXXXX, so the file shows all it holds."""
    out = []
    for c in text:
        if c in '"\\':
            out.append("\\" + c)
        elif not c.isprintable():
            out.append(f"\\u{ord(c):04x}" if ord(c) < 0x10000 else f"\\U{ord(c):08x}")
        else:
            out.append(c)
    return '"' + "".join(out) + '"'


def toml_key(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else toml_string(key)


def label(*keys: str) -> str:
    """How a problem names the table it is in, as the file spells its header: [sessions."/home/me/app"]."""
    return "[" + ".".join(toml_key(k) for k in keys) + "]"


@dataclass(frozen=True)
class RepoOptions:
    handover_paths: tuple[str, ...] = DEFAULT_HANDOVER_PATHS
    push_deploys: bool = False


@dataclass(frozen=True)
class RepoRules:
    access: str  # "read" | "write"
    handover_paths: tuple[str, ...]
    push_deploys: bool


@dataclass(frozen=True)
class Session:
    folder: Path  # resolved
    access: dict[str, str]  # lower-case "owner/name" -> "read" | "write", the strongest of its grants
    via: dict[str, tuple[str, ...]]  # lower-case "owner/name" -> what grants it: "repo", "write", "read @group"
    names: dict[str, str]  # lower-case "owner/name" -> as the file first spells it


@dataclass(frozen=True)
class Problem:
    """Something wrong with a rules file, or worth a warning. `where` is the TOML key path it is
    about, outermost first, so `gitgaterules check` can find its line."""

    text: str
    where: tuple[str, ...] = ()


@dataclass(frozen=True)
class Rules:
    workspaces: tuple[Path, ...]
    repos: dict[str, RepoRules]  # lower-case "owner/name"; in v2, filled in only by `scoped`
    protected_branches: tuple[str, ...] = ("main", "master")  # fnmatchcase patterns: "*" crosses "/"
    git_timeout: int = 300
    gh_timeout: int = 1800
    forbidden: tuple[Path, ...] = field(default=())
    version: int = 1
    sessions: tuple[Session, ...] = ()
    options: dict[str, RepoOptions] = field(default_factory=dict)  # v2 [repos], lower-case "owner/name"
    session: Session | None = None  # the calling session, once `scoped`

    def repo(self, repo: str) -> RepoRules:
        if not valid_repo(repo):
            raise Refused(f"'{repo}' is not an owner/name repository.")
        rules = self.repos.get(repo.lower())
        if rules is None:
            if self.version == 2:
                raise Refused(self._denied(repo, "has no access to", "read"))
            raise Refused(f"{repo} is not on the git-gate repo allowlist (rules.toml [repos]).")
        return rules

    def access(self, repo: str) -> str:
        return self.repo(repo).access

    def require_write(self, repo: str) -> None:
        if self.access(repo) != "write":
            if self.version == 2:
                raise Refused(self._denied(repo, "can only read", "write"))
            raise Refused(f"{repo} is read-only in rules.toml; this call needs write access.")

    def _denied(self, repo: str, what: str, level: str) -> str:
        if self.session is None:
            return f"{repo}: no git-gate session is in effect, so there is no access to any repo."
        folder = self.session.folder
        return (
            f"This session ({folder}) {what} {repo}: git-gate keeps each session to its own repo and the "
            f"repos the user grants it. Do not work around this. Only the user can grant {level} access, "
            f"from their own terminal:\n\ngitgaterules grant {folder} {level} {repo}"
        )

    def scoped(self, cwd: str | None) -> Rules:
        """The rules for a call from a conversation whose project folder is `cwd`. v1 rules are not
        scoped. v2 rules become those of the session with the longest folder holding `cwd`: its
        repos at its access, and its folder as the only workspace. No such session is refused."""
        if self.version == 1:
            return self
        session = self.session_for(cwd)
        if session is None:
            raise Refused(_no_session(cwd))
        repos = {}
        for name, level in session.access.items():
            opts = self.options.get(name, RepoOptions())
            repos[name] = RepoRules(level, opts.handover_paths, opts.push_deploys)
        return replace(self, workspaces=(session.folder,), repos=repos, session=session)

    def session_for(self, cwd: str | None) -> Session | None:
        if cwd is None or not Path(cwd).is_absolute():
            return None
        found = [s for s in self.sessions if Path(cwd).is_relative_to(s.folder)]
        return max(found, key=lambda s: len(s.folder.parts), default=None)

    def level(self, repo: object) -> str | None:
        """The calling session's access to `repo` for audit.log: "write", "read" or "none"; None
        for v1 rules, or rules not yet scoped, or a call without a repo."""
        if self.session is None or repo is None:
            return None
        rules = self.repos.get(repo.lower()) if isinstance(repo, str) else None
        return rules.access if rules else "none"


def _no_session(cwd: str | None) -> str:
    if cwd is None:
        return (
            "git-gate cannot tell which project folder this conversation runs in (it may have been moved "
            "or deleted), so it has no access to any repo. Do not work around this. Only the user can "
            "grant access, with gitgaterules, from their own terminal."
        )
    return (
        f"This conversation's project folder, {cwd}, is not a git-gate session, so it has no access to "
        "any repo. Do not work around this. Only the user can grant access, from their own terminal:\n\n"
        f"gitgaterules add-session {cwd} --repo <owner/name>"
    )


@dataclass(frozen=True)
class Checked:
    rules: Rules | None  # None when there are problems
    problems: list[Problem]
    warnings: list[Problem]


def _parse_options(entry: dict) -> RepoOptions:
    extra = entry.get("handover_paths", [])
    if not isinstance(extra, list) or not all(isinstance(p, str) for p in extra):
        raise Refused("handover_paths must be a list of path patterns.")
    patterns = tuple(canonical_pattern(p) for p in extra)
    push_deploys = entry.get("push_deploys", False)
    if not isinstance(push_deploys, bool):
        raise Refused("push_deploys must be true or false.")
    return RepoOptions(tuple(dict.fromkeys(DEFAULT_HANDOVER_PATHS + patterns)), push_deploys)


def _parse_repo(name: str, entry: object) -> RepoRules:
    if isinstance(entry, str):
        entry = {"access": entry}
    if not isinstance(entry, dict):
        raise Refused(f"bad repo entry {name!r} = {entry!r}.")
    unknown = sorted(set(entry) - set(REPO_KEYS))
    if unknown:
        raise Refused(f"repo {name!r} has unknown keys {unknown}; allowed: {list(REPO_KEYS)}.")
    if entry.get("access") not in ACCESS_LEVELS:
        raise Refused(f'repo {name!r} needs access = "read" or "write".')
    try:
        opts = _parse_options(entry)
    except Refused as e:
        raise Refused(f"repo {name!r}: {e}") from None
    return RepoRules(entry["access"], opts.handover_paths, opts.push_deploys)


def _strings(data: dict, key: str, default: tuple[str, ...], problems: list[Problem]) -> tuple[str, ...]:
    value = data.get(key, list(default))
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        problems.append(Problem(f"{key} must be a list of non-empty strings, got {value!r}.", (key,)))
        return default
    return tuple(value)


def _timeout(data: dict, key: str, default: int, problems: list[Problem]) -> int:
    value = data.get(key, default)
    if type(value) is not int or not 1 <= value <= MAX_TIMEOUT:
        problems.append(
            Problem(f"{key} must be a whole number of seconds from 1 to {MAX_TIMEOUT}, got {value!r}.", (key,))
        )
        return default
    return value


def _settings(data: dict, problems: list[Problem]) -> dict:
    return {
        "protected_branches": _strings(data, "protected_branches", ("main", "master"), problems),
        "git_timeout": _timeout(data, "git_timeout_seconds", 300, problems),
        "gh_timeout": _timeout(data, "gh_timeout_seconds", 1800, problems),
    }


def _table(data: dict, key: str, problems: list[Problem]) -> dict:
    value = data.get(key, {})
    if not isinstance(value, dict):
        problems.append(Problem(f"{key} must be a table, got {value!r}.", (key,)))
        return {}
    return value


def check_enabled(cfg: Path | None = None) -> None:
    """Refuse while <config>/disabled exists in any form: a dangling symlink still switches off."""
    if os.path.lexists((cfg or config_dir()) / "disabled"):
        raise Refused(OFF_SWITCH_MESSAGE)


def check_rules(text: str, cfg: Path | None = None, extra_forbidden: tuple[Path, ...] = ()) -> Checked:
    """Every problem with a rules file's text, and the rules if there are none. The gate and
    gitgaterules both read rules through here, so a file gitgaterules writes is one the gate loads."""
    cfg = cfg or config_dir()
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        return Checked(None, [Problem(f"the file is not valid TOML: {e}")], [])
    forbidden = tuple(p.resolve() for p in (cfg, state_dir(), Path("/opt/git-gate"), *extra_forbidden))
    problems: list[Problem] = []
    warnings: list[Problem] = []
    parse = _parse_v2 if "sessions" in data else _parse_v1
    rules = parse(data, forbidden, problems, warnings)
    return Checked(None if problems else rules, problems, warnings)


def _parse_v1(data: dict, forbidden: tuple[Path, ...], problems: list[Problem], warnings: list[Problem]) -> Rules:
    unknown = sorted(set(data) - set(TOP_KEYS))
    if unknown:
        hint = " A file without [sessions] is v1, which has no [groups]." if "groups" in unknown else ""
        problems.append(Problem(f"unknown top-level keys {unknown}; allowed: {list(TOP_KEYS)}.{hint}"))
    table = data.get("repos", {})
    if not isinstance(table, dict):
        problems.append(Problem("repos must be a table of owner/name entries.", ("repos",)))
        table = {}
    repos = {}
    for name, entry in table.items():
        if not REPO_RE.fullmatch(name):
            problems.append(Problem(f"bad repo entry {name!r} = {entry!r}.", ("repos", name)))
        elif name.lower() in repos:
            problems.append(Problem(f"repo {name!r} is listed twice (repo names ignore case).", ("repos", name)))
        else:
            try:
                repos[name.lower()] = _parse_repo(name, entry)
            except Refused as e:
                problems.append(Problem(str(e), ("repos", name)))

    workspaces = []
    for w in _strings(data, "workspaces", (), problems):
        wp = Path(w)
        if not wp.is_absolute():
            problems.append(Problem(f"workspace {w!r} must be absolute.", ("workspaces",)))
            continue
        wp = wp.resolve()
        for f in forbidden:
            if wp.is_relative_to(f) or f.is_relative_to(wp):
                problems.append(Problem(f"workspace {wp} overlaps gate-owned {f}.", ("workspaces",)))
                break
        else:
            workspaces.append(wp)
    warnings.append(
        Problem(
            "this is a v1 rules file (it has no [sessions]), which is not scoped: every conversation reaches "
            "every repo in [repos] and every workspace. Run gitgaterules migrate to give each session its own."
        )
    )
    return Rules(workspaces=tuple(workspaces), repos=repos, forbidden=forbidden, **_settings(data, problems))


def _parse_v2(data: dict, forbidden: tuple[Path, ...], problems: list[Problem], warnings: list[Problem]) -> Rules:
    for key in sorted(set(data) - set(TOP_KEYS_V2)):
        if key == "workspaces":
            text = "workspaces is not used in a file with [sessions]: each session's folder is its workspace."
        else:
            text = f"unknown top-level key {key!r}; a file with [sessions] takes {list(TOP_KEYS_V2)}."
        problems.append(Problem(text, (key,)))
    settings = _settings(data, problems)
    groups = _parse_groups(_table(data, "groups", problems), problems)
    options = _parse_repo_options(_table(data, "repos", problems), problems)
    sessions: list[Session] = []
    seen: dict[Path, str] = {}
    for folder, entry in _table(data, "sessions", problems).items():
        session = _parse_session(folder, entry, groups, forbidden, problems)
        if session is None:
            continue
        if session.folder in seen:
            problems.append(
                Problem(
                    f"{label('sessions', seen[session.folder])} and {label('sessions', folder)} are the same "
                    f"folder, {session.folder}; keep one.",
                    ("sessions", folder),
                )
            )
            continue
        seen[session.folder] = folder
        sessions.append(session)
        if not os.path.lexists(session.folder / ".git"):
            warnings.append(
                Problem(
                    f"{label('sessions', folder)}: the folder {session.folder} is not a git checkout (it has no .git).",
                    ("sessions", folder),
                )
            )
    reached = {name for s in sessions for name in s.access}
    for name in options:
        if name not in reached and not problems:  # a session with problems reaches nothing yet
            warnings.append(
                Problem(f"{label('repos', name)} sets options for a repo no session reaches.", ("repos", name))
            )
    return Rules(
        workspaces=(), repos={}, forbidden=forbidden, version=2, sessions=tuple(sessions), options=options, **settings
    )


def _entries(where: tuple[str, ...], value: object, problems: list[Problem]) -> list[str]:
    """A list of owner/name repos and @group names, each checked for form only."""
    named = f"{label(*where[:-1])} {where[-1]}"
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        problems.append(Problem(f"{named} must be a list of owner/name repos and @groups, got {value!r}.", where))
        return []
    good = []
    for entry in value:
        if entry.startswith("@") or valid_repo(entry):
            good.append(entry)
        else:
            problems.append(Problem(f"{named}: {entry!r} is not an owner/name repo or an @group.", where))
    return good


def _parse_groups(table: dict, problems: list[Problem]) -> dict[str, tuple[str, ...]]:
    """Each group's repos, nested groups expanded, as first spelled; a cycle is a problem."""
    raw: dict[str, list[str]] = {}
    for name, value in table.items():
        if not GROUP_RE.fullmatch(name):
            problems.append(
                Problem(f"[groups] {name!r} is not a group name: use letters, digits, - and _.", ("groups", name))
            )
            continue
        raw[name] = _entries(("groups", name), value, problems)
    for name, entries in raw.items():
        for entry in entries:
            if entry.startswith("@") and entry[1:] not in raw:
                problems.append(Problem(f"[groups] {name}: {entry!r} names no group in [groups].", ("groups", name)))

    expanded: dict[str, tuple[str, ...]] = {}
    cycles: set[frozenset[str]] = set()

    def expand(name: str, path: list[str]) -> tuple[str, ...]:
        if name in expanded:
            return expanded[name]
        out: dict[str, str] = {}
        for entry in raw[name]:
            if not entry.startswith("@"):
                out.setdefault(entry.lower(), entry)
                continue
            inner = entry[1:]
            if inner not in raw:
                continue
            if inner in path:
                cycle = path[path.index(inner) :] + [inner]
                if frozenset(cycle) not in cycles:
                    cycles.add(frozenset(cycle))
                    through = " -> ".join(f"@{g}" for g in cycle[1:])
                    problems.append(
                        Problem(
                            f"[groups] {cycle[0]}: the group includes itself through {through}.", ("groups", cycle[0])
                        )
                    )
                continue
            for repo in expand(inner, path + [inner]):
                out.setdefault(repo.lower(), repo)
        expanded[name] = tuple(out.values())
        return expanded[name]

    for name in raw:
        expand(name, [name])
    return expanded


def _parse_repo_options(table: dict, problems: list[Problem]) -> dict[str, RepoOptions]:
    options: dict[str, RepoOptions] = {}
    for name, entry in table.items():
        named = label("repos", name)
        where = ("repos", name)
        if not valid_repo(name):
            problems.append(Problem(f"{named} is not an owner/name repo.", where))
            continue
        if name.lower() in options:
            problems.append(Problem(f"{named} is listed twice (repo names ignore case).", where))
            continue
        if not isinstance(entry, dict):
            problems.append(
                Problem(
                    f"{named} must be a table of handover_paths and push_deploys, got {entry!r}; with [sessions], "
                    "access is set per session.",
                    where,
                )
            )
            continue
        if "access" in entry:
            problems.append(
                Problem(f"{named} has access; with [sessions], access is set per session (repo, read, write).", where)
            )
        unknown = sorted(set(entry) - {"access", *OPTION_KEYS})
        if unknown:
            problems.append(Problem(f"{named} has unknown keys {unknown}; allowed: {list(OPTION_KEYS)}.", where))
        try:
            options[name.lower()] = _parse_options(entry)
        except Refused as e:
            problems.append(Problem(f"{named}: {e}", where))
    return options


def _parse_session(
    folder: str, entry: object, groups: dict[str, tuple[str, ...]], forbidden: tuple[Path, ...], problems: list[Problem]
) -> Session | None:
    named = label("sessions", folder)
    where = ("sessions", folder)
    count = len(problems)
    real = None
    if not folder.isprintable():
        problems.append(Problem(f"{named}: the folder name has unprintable characters.", where))
    elif not Path(folder).is_absolute():
        problems.append(Problem(f"{named}: the folder must be an absolute path.", where))
    else:
        real = Path(folder).resolve()
        for f in forbidden:
            if real.is_relative_to(f) or f.is_relative_to(real):
                problems.append(Problem(f"{named}: the folder {real} overlaps gate-owned {f}.", where))
                break
        if not real.is_dir():
            problems.append(
                Problem(
                    f"{named}: the folder {real} does not exist or is not a directory. If the project is gone, "
                    f"remove the session with: gitgaterules remove-session {folder}",
                    where,
                )
            )
    if not isinstance(entry, dict):
        problems.append(Problem(f"{named} must be a table of repo, read and write, got {entry!r}.", where))
        return None
    unknown = sorted(set(entry) - set(SESSION_KEYS))
    if unknown:
        problems.append(Problem(f"{named} has unknown keys {unknown}; a session takes repo, read and write.", where))

    access: dict[str, str] = {}
    via: dict[str, list[str]] = {}
    names: dict[str, str] = {}

    def grant(repo: str, level: str, source: str) -> None:
        key = repo.lower()
        access[key] = "write" if "write" in (level, access.get(key)) else "read"
        via.setdefault(key, []).append(source)
        names.setdefault(key, repo)

    own = entry.get("repo")
    if own is not None:
        if valid_repo(own):
            grant(own, "write", "repo")
        else:
            problems.append(Problem(f"{named} repo must be one owner/name repo, got {own!r}.", (*where, "repo")))
    lists = {level: _entries((*where, level), entry.get(level, []), problems) for level in ("write", "read")}
    for repo in sorted({e.lower() for e in lists["read"]} & {e.lower() for e in lists["write"]}):
        problems.append(
            Problem(f"{named}: {repo} is in both read and write; list it in one (write includes read).", where)
        )
    for level in ("write", "read"):
        for entry_ in lists[level]:
            if not entry_.startswith("@"):
                grant(entry_, level, level)
            elif entry_[1:] in groups:
                for repo in groups[entry_[1:]]:
                    grant(repo, level, f"{level} {entry_}")
            else:
                problems.append(Problem(f"{named} {level}: {entry_!r} names no group in [groups].", (*where, level)))
    if real is None or len(problems) > count:
        return None
    return Session(real, access, {k: tuple(v) for k, v in via.items()}, names)


def load_rules(cfg: Path | None = None, extra_forbidden: tuple[Path, ...] = ()) -> Rules:
    cfg = cfg or config_dir()
    path = cfg / "rules.toml"
    try:
        text = path.read_text()
    except FileNotFoundError:
        raise Refused(f"No rules file at {path}; the user has to create it.") from None
    except UnicodeDecodeError:
        raise Refused(f"{path} is not UTF-8 text.") from None
    checked = check_rules(text, cfg, extra_forbidden)
    if checked.problems:
        raise Refused("\n".join(f"{path}: {p.text}" for p in checked.problems))
    return checked.rules


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
