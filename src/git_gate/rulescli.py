"""gitgaterules: check, show and edit ~/.config/git-gate/rules.toml from the user's own terminal.

The user runs it outside the sandbox; the gate exposes no tool for it. It reads rules through the
same `check_rules` the gate loads them with, so what it accepts is what the gate accepts.
"""

import difflib
import os
import re
import secrets
import subprocess
import sys
import time
import tomllib
from pathlib import Path

from git_gate import Refused
from git_gate.config import (
    OPTION_KEYS,
    SESSION_KEYS,
    SETTINGS,
    Checked,
    Rules,
    canonical_pattern,
    check_rules,
    config_dir,
    label,
    toml_key,
    toml_string,
    valid_repo,
)

USAGE = """\
usage: gitgaterules <command>

  check [FILE]                             check rules.toml (or FILE) and list every problem
  show                                     each session's repos, read or write, groups expanded
  grant <folder> read|write <repo|@group>  let a session read or write a repo or a group's repos
  revoke <folder> <repo|@group>            take a grant (or the session's own repo) away
  add-session <folder> [--repo <repo>]     make a project folder a session, with its own repo
  remove-session <folder>                  remove a session
  group <name> add|remove <repo|@group>    change a group, making it if it is new
  migrate [--write]                        turn a v1 file into sessions; --write writes it

A session is a project folder: a conversation started in it, or below it, reaches the session's
own repo (write), its grants, and no other repo. Every edit is checked as the gate checks the
file, keeps the old file as rules.toml.bak-<time>, and prints the change.
"""
HEADER_RE = re.compile(r"\s*\[(?!\[)(.*)\]\s*(?:#.*)?")
KEY_RE = re.compile(
    r"""\s*((?:[A-Za-z0-9_-]+|"(?:[^"\\]|\\.)*"|'[^']*')(?:\s*\.\s*(?:[A-Za-z0-9_-]+|"(?:[^"\\]|\\.)*"|'[^']*'))*)\s*="""
)


def rules_path() -> Path:
    return config_dir() / "rules.toml"


def _keys(toml: str) -> tuple[str, ...] | None:
    """The key path a header or key line names, read by tomllib itself."""
    try:
        node = tomllib.loads(toml)
    except tomllib.TOMLDecodeError:
        return None
    keys = []
    while isinstance(node, dict) and len(node) == 1:
        key, node = next(iter(node.items()))
        keys.append(key)
    return tuple(keys)


def line_of(text: str, where: tuple[str, ...]) -> int | None:
    """The line that best matches `where`: the header or key line naming most of it."""
    if not where:
        return None
    table: tuple[str, ...] = ()
    best, best_len = None, 0
    for n, line in enumerate(text.splitlines(), 1):
        if m := HEADER_RE.fullmatch(line):
            keys = _keys(f"[{m[1]}]")
            if keys is None:
                continue
            table = keys
        elif m := KEY_RE.match(line):
            inner = _keys(f"{m[1]} = 0")
            if inner is None:
                continue
            keys = table + inner
        else:
            continue
        if keys == where[: len(keys)] and len(keys) > best_len:
            best, best_len = n, len(keys)
    return best


def _say_problems(checked: Checked, text: str, say) -> None:
    for kind, found in (("error", checked.problems), ("warning", checked.warnings)):
        for p in found:
            n = line_of(text, p.where)
            say(f"{kind}: {f'line {n}: ' if n else ''}{p.text}")


def _read(path: Path, say) -> str | None:
    try:
        return path.read_text()
    except FileNotFoundError:
        say(f"No rules file at {path}.")
    except (OSError, UnicodeDecodeError) as e:
        say(f"Cannot read {path}: {e}.")
    return None


def _load(path: Path, say) -> tuple[str, Checked] | None:
    text = _read(path, say)
    if text is None:
        return None
    return text, check_rules(text, config_dir())


def _reached(rules: Rules) -> set[str]:
    return {name for s in rules.sessions for name in s.access}


def cmd_check(args: list[str], say) -> int:
    if len(args) > 1:
        say(USAGE, end="")
        return 2
    loaded = _load(Path(args[0]) if args else rules_path(), say)
    if loaded is None:
        return 1
    text, checked = loaded
    _say_problems(checked, text, say)
    if checked.problems:
        say(f"{len(checked.problems)} problem(s): while rules.toml has any, the gate refuses every call.")
        return 1
    rules = checked.rules
    if rules.version == 1:
        say(f"OK: v1 file, {len(rules.workspaces)} workspaces, {len(rules.repos)} repos")
    else:
        say(f"OK: {len(rules.sessions)} sessions, {len(_reached(rules))} repos")
    return 0


VIA = {"repo": "own repo"}


def cmd_show(args: list[str], say) -> int:
    if args:
        say(USAGE, end="")
        return 2
    loaded = _load(rules_path(), say)
    if loaded is None:
        return 1
    text, checked = loaded
    if checked.problems:
        _say_problems(checked, text, say)
        return 1
    rules = checked.rules
    if rules.version == 1:
        say("v1 rules, not scoped: every conversation reaches all of these.")
        say("workspaces:")
        for w in rules.workspaces:
            say(f"  {w}")
        say("repos:")
        for name, repo in sorted(rules.repos.items()):
            say(f"  {repo.access:<5}  {name}")
        return 0
    blocks = []
    for s in sorted(rules.sessions, key=lambda s: str(s.folder)):
        lines = [str(s.folder)]
        width = max((len(n) for n in s.names.values()), default=0)
        for key in sorted(s.access, key=lambda k: (s.access[k] != "write", k)):
            via = ", ".join(VIA.get(v, v) for v in s.via[key])
            lines.append(f"  {s.access[key]:<5}  {s.names[key]:<{width}}  {via}")
        if len(lines) == 1:
            lines.append("  (no repos)")
        blocks.append("\n".join(lines))
    say("\n\n".join(blocks) if blocks else "(no sessions)")
    return 0


# --- writing -----------------------------------------------------------------------------------

HEADER = """\
# Managed by gitgaterules, which rewrites this file in one fixed order and keeps no comments.
# Change it with gitgaterules (gitgaterules --help), or by hand and then run gitgaterules check.
"""
INLINE = 100  # a list longer than this on one line gets one entry per line
TABLE_ORDER = ("groups", "sessions", "repos")
KEY_ORDER = {"sessions": SESSION_KEYS, "repos": OPTION_KEYS}


class EditError(Exception):
    """An edit that cannot be made; the message says why, and nothing is written."""


def _sort_key(entry: object) -> tuple:
    return (str(entry).casefold(), str(entry))


def _repo_entry(entry: str) -> str:
    return entry if entry.startswith("@") else entry.lower()


def _pattern(entry: str) -> str:
    try:
        return canonical_pattern(entry)
    except Refused:
        return entry  # kept as written, for the check to refuse


def _sameness(table: str, key: str):
    """How the gate compares this list's entries: dedup must never merge two it tells apart.
    Repo names ignore case; handover paths are compared in canonical form; anything else
    (protected_branches is matched case-sensitively) only when identical."""
    if table == "groups" or (table == "sessions" and key in ("read", "write")):
        return _repo_entry
    if table == "repos" and key == "handover_paths":
        return _pattern
    return str


def _items(value: list, same, where: str) -> list[str]:
    if not all(isinstance(v, str) for v in value):
        return [_value(v, same, where) for v in value]
    seen: dict[str, str] = {}
    for v in sorted(value, key=_sort_key):
        seen.setdefault(same(v), v)
    return [toml_string(v) for v in seen.values()]


def _value(value: object, same, where: str) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return toml_string(value)
    if isinstance(value, list):
        return "[" + ", ".join(_items(value, same, where)) + "]"
    raise EditError(f"gitgaterules cannot rewrite {where} = {value!r}; fix it by hand, then run gitgaterules check.")


def _line(table: str, key: str, value: object, where: str) -> str:
    same = _sameness(table, key)
    text = f"{toml_key(key)} = {_value(value, same, where)}"
    if len(text) <= INLINE or not isinstance(value, list) or not value:
        return text
    return f"{toml_key(key)} = [\n" + "".join(f"    {v},\n" for v in _items(value, same, where)) + "]"


def _entries_of(table: str, entries: dict, order: tuple[str, ...], where: str) -> list[str]:
    rank = {k: i for i, k in enumerate(order)}
    lines = []
    for key in sorted(entries, key=lambda k: (rank.get(k, len(rank)), k)):
        value = entries[key]
        if isinstance(value, dict):
            raise EditError(f"gitgaterules cannot rewrite the table {where}.{key}; fix it by hand.")
        if isinstance(value, list) and not value and key in ("read", "write"):
            continue
        lines.append(_line(table, key, value, f"{where}.{key}"))
    return lines


def render(data: dict) -> str:
    """The canonical text of a v2 rules file: fixed order, sorted lists, no comments."""
    out = [HEADER]
    scalars = [k for k in data if not isinstance(data[k], dict)]
    if scalars:
        out.append("\n".join(_entries_of("", {k: data[k] for k in scalars}, SETTINGS, "top level")) + "\n")
    rank = {k: i for i, k in enumerate(TABLE_ORDER)}
    for name in sorted((k for k in data if isinstance(data[k], dict)), key=lambda k: (rank.get(k, len(rank)), k)):
        table = data[name]
        if name in ("sessions", "repos"):
            if not table and name == "sessions":
                out.append("[sessions]\n")  # an empty [sessions] still makes the file v2: nothing is reachable
            for key in sorted(table, key=_sort_key):
                entry = table[key]
                if not isinstance(entry, dict):
                    raise EditError(f"gitgaterules cannot rewrite {label(name, key)} = {entry!r}; fix it by hand.")
                body = _entries_of(name, entry, KEY_ORDER[name], label(name, key))
                out.append("\n".join([label(name, key), *body]) + "\n")
        elif table:
            out.append("\n".join([label(name), *_entries_of(name, table, (), label(name))]) + "\n")
    return "\n".join(out)


def _stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%S")


def _write_new(path: Path, data: bytes) -> None:
    """A new file (0600) holding `data`, on disk before this returns."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def _backup(path: Path, data: bytes) -> Path:
    base = f"{path.name}.bak-{_stamp()}"
    name, n = base, 0
    while os.path.lexists(path.with_name(name)):
        n += 1
        name = f"{base}.{n}"
    _write_new(path.with_name(name), data)
    return path.with_name(name)


def _replace(path: Path, text: str) -> None:
    """Write `text` over `path` atomically: a temp file beside it, fsynced, renamed over it."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(6)}.new")
    try:
        _write_new(tmp, text.encode())
        os.rename(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    dfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def save(path: Path, old: bytes | None, new: str, say) -> tuple[int, Checked | None]:
    """Validate `new` as the gate would, keep a backup of `old`, write `new` and show the diff."""
    old_text = old.decode() if old is not None else ""
    if old_text == new:
        say(f"No change: {path} already says so.")
        return 0, None
    checked = check_rules(new, config_dir())
    if checked.problems:
        _say_problems(Checked(None, checked.problems, []), new, say)
        say(f"Not written: the result would not load, so {path} is unchanged.")
        return 1, None
    target = path.resolve()  # a symlinked rules.toml (dotfiles) is written through, not replaced
    now = target.read_bytes() if target.exists() else None
    if now != old:
        say(f"Not written: {path} changed while gitgaterules was editing it; run the command again.")
        return 1, None
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = _backup(target, old) if old is not None else None
    _replace(target, new)
    diff = difflib.unified_diff(
        old_text.splitlines(keepends=True), new.splitlines(keepends=True), f"{path} (before)", str(path)
    )
    say("".join(diff), end="")
    if backup:
        say(f"Backup of the old file: {backup}")
    _say_problems(Checked(None, [], checked.warnings), new, say)
    say(f"Wrote {path}.")
    return 0, checked


def _edit(say, change) -> tuple[int, Checked | None]:
    """Apply `change` to the parsed v2 file (a missing file starts empty) and save the result.
    The current file need not be valid, so an edit can mend it; the result must be."""
    path = rules_path()
    try:
        old = path.read_bytes()
    except FileNotFoundError:
        old = None
    try:
        data = tomllib.loads(old.decode()) if old is not None else {}
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        say(f"{path} cannot be read as TOML ({e}); fix it by hand, then run gitgaterules check.")
        return 1, None
    if old is not None and "sessions" not in data:
        say(f"{path} is a v1 file; gitgaterules edits v2 files. Run gitgaterules migrate first.")
        return 1, None
    data.setdefault("sessions", {})
    try:
        note = change(data)
        new = render(data)
    except EditError as e:
        say(str(e))
        return 1, None
    if note:
        say(note)
    return save(path, old, new, say)


def _same_folder(a: str, b: str) -> bool:
    norm = os.path.normpath
    return a == b or norm(os.path.abspath(a)) == norm(os.path.abspath(b)) or Path(a).resolve() == Path(b).resolve()


def _session(data: dict, folder: str) -> tuple[str, dict]:
    sessions = data["sessions"]
    if not isinstance(sessions, dict):
        raise EditError("sessions is not a table; fix it by hand, then run gitgaterules check.")
    for key, entry in sessions.items():
        if _same_folder(key, folder):
            if not isinstance(entry, dict):
                raise EditError(f"{label('sessions', key)} is not a table; fix it by hand.")
            return key, entry
    raise EditError(f"No session for {folder}. Add it first: gitgaterules add-session {folder} --repo <owner/name>")


def _target(target: str) -> str:
    if target.startswith("@") or valid_repo(target):
        return target
    raise EditError(f"{target!r} is neither an owner/name repo nor an @group.")


def _drop(entries: object, target: str) -> bool:
    """Remove `target` (repo names ignore case) from a list in place; True if it was there."""
    if not isinstance(entries, list):
        return False
    same = [e for e in entries if isinstance(e, str) and _same_entry(e, target)]
    for e in same:
        entries.remove(e)
    return bool(same)


def _same_entry(a: str, b: str) -> bool:
    return a == b if a.startswith("@") or b.startswith("@") else a.lower() == b.lower()


def _usage(say) -> int:
    say(USAGE, end="")
    return 2


def cmd_grant(args: list[str], say) -> int:
    if len(args) != 3 or args[1] not in ("read", "write"):
        return _usage(say)
    folder, level, target = args
    other = "read" if level == "write" else "write"

    def change(data: dict) -> str:
        key, entry = _session(data, folder)
        _target(target)
        moved = _drop(entry.get(other), target)
        entries = entry.setdefault(level, [])
        if not isinstance(entries, list):
            raise EditError(f"{label('sessions', key)} {level} is not a list; fix it by hand.")
        if not any(isinstance(e, str) and _same_entry(e, target) for e in entries):
            entries.append(target)
        return f"{key}: {target} moved from {other} to {level}." if moved else ""

    return _edit(say, change)[0]


def cmd_revoke(args: list[str], say) -> int:
    if len(args) != 2:
        return _usage(say)
    folder, target = args
    found: list[str] = []

    def change(data: dict) -> str:
        key, entry = _session(data, folder)
        found.append(key)
        dropped = [level for level in ("write", "read") if _drop(entry.get(level), target)]
        own = entry.get("repo")
        if isinstance(own, str) and _same_entry(own, target):
            del entry["repo"]
            dropped.append("repo")
        if not dropped:
            raise EditError(f"{target} is not granted to {key} by name; gitgaterules show lists what reaches what.")
        return ""

    code, checked = _edit(say, change)
    if checked and not target.startswith("@"):
        session = next(s for s in checked.rules.sessions if _same_folder(str(s.folder), found[0]))
        if target.lower() in session.access:
            via = ", ".join(session.via[target.lower()])
            say(f"Note: {target} is still reached through {via} ({session.access[target.lower()]}).")
    return code


def cmd_add_session(args: list[str], say) -> int:
    if len(args) not in (1, 3) or (len(args) == 3 and args[1] != "--repo"):
        return _usage(say)
    folder = str(Path(args[0]).resolve())

    def change(data: dict) -> str:
        try:
            key, _ = _session(data, args[0])
        except EditError:
            pass
        else:
            raise EditError(f"{key} is already a session; gitgaterules grant adds to it.")
        data["sessions"][folder] = {"repo": _target(args[2])} if len(args) == 3 else {}
        return f"The session is stored as {folder}." if folder != args[0] else ""

    return _edit(say, change)[0]


def cmd_remove_session(args: list[str], say) -> int:
    if len(args) != 1:
        return _usage(say)

    def change(data: dict) -> str:
        key, _ = _session(data, args[0])
        del data["sessions"][key]
        return ""

    return _edit(say, change)[0]


def cmd_group(args: list[str], say) -> int:
    if len(args) != 3 or args[1] not in ("add", "remove"):
        return _usage(say)
    name, action, target = args

    def change(data: dict) -> str:
        groups = data.setdefault("groups", {})
        if not isinstance(groups, dict):
            raise EditError("groups is not a table; fix it by hand, then run gitgaterules check.")
        _target(target)
        if action == "add":
            entries = groups.setdefault(name, [])
            if not isinstance(entries, list):
                raise EditError(f"[groups] {name} is not a list; fix it by hand.")
            if not any(isinstance(e, str) and _same_entry(e, target) for e in entries):
                entries.append(target)
        elif not _drop(groups.get(name), target):
            raise EditError(f"{target} is not in [groups] {name}.")
        return ""

    return _edit(say, change)[0]


# --- migrate -----------------------------------------------------------------------------------

GIT = "/usr/bin/git"
GITHUB_URL_RE = re.compile(
    r"(?:git@github\.com:|ssh://git@github\.com(?::22)?/|https://(?:[^@/]+@)?github\.com/|git://github\.com/)"
    r"([^/]+/[^/]+?)(?:\.git)?/?"
)


def _shown_url(url: str) -> str:
    """A remote URL fit to print: any user name and password in it removed."""
    return re.sub(r"//[^@/]*@", "//", url)


def origin_repo(folder: Path) -> tuple[str | None, str]:
    """(owner/name, "") for a checkout whose origin is on github.com, else (None, why not).

    Only the checkout's own config is read: no system or global config (where an insteadOf or an
    origin of its own could name another repo), and no walking up into a parent checkout.
    """
    if not os.path.lexists(folder / ".git"):
        return None, "is not a git checkout (it has no .git)"
    env = {
        "PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "LANG": "C.UTF-8", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CEILING_DIRECTORIES": str(folder.parent),
        "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0",
    }  # fmt: skip
    try:
        proc = subprocess.run(
            [GIT, "-C", str(folder), "config", "--get", "remote.origin.url"],
            env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace", timeout=30,
            check=False,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"could not be read by git ({e})"
    if proc.returncode == 1:
        return None, "has no origin remote"
    if proc.returncode != 0:
        return None, f"could not be read by git ({proc.stderr.strip()[-300:]})"
    url = proc.stdout.strip()
    m = GITHUB_URL_RE.fullmatch(url)
    if not m or not valid_repo(m[1]):
        return None, f"has origin {_shown_url(url)}, which is not a github.com repository"
    return m[1], ""


def migrate(data: dict) -> tuple[dict, list[str]]:
    """A v2 version of a valid v1 file, and every judgement made on the way, in plain words."""
    notes: list[str] = []
    access: dict[str, str] = {}
    names: dict[str, str] = {}
    options: dict[str, dict] = {}
    for name, entry in data.get("repos", {}).items():
        level = entry if isinstance(entry, str) else entry["access"]
        access[name.lower()], names[name.lower()] = level, name
        kept = {k: v for k, v in entry.items() if k != "access"} if isinstance(entry, dict) else {}
        if kept:
            options[name] = kept
    reads = sorted((n for n, level in access.items() if level == "read"), key=_sort_key)
    new: dict = {k: v for k, v in data.items() if k in SETTINGS}
    if reads:
        new["groups"] = {"reads": [names[n] for n in reads]}
        notes.append(
            f"Group reads holds the {len(reads)} read-only repo(s) of [repos]; every session reads it, "
            "as every conversation can today."
        )
    sessions: dict[str, dict] = {}
    owned: set[str] = set()
    for w in data.get("workspaces", []):
        folder = Path(w).resolve()
        key = str(folder)
        if key in sessions:
            notes.append(f"{w}: the same folder as an earlier workspace, {key}; one session.")
            continue
        if not folder.is_dir():
            notes.append(f"{w}: left out: the folder does not exist, and a session's folder must.")
            continue
        entry: dict = {}
        repo, why = origin_repo(folder)
        level = access.get(repo.lower()) if repo else None
        if repo is None:
            notes.append(f"{key}: {why}, so the session has no repo of its own; it gets the reads only.")
        elif level == "write":
            entry["repo"] = repo
            owned.add(repo.lower())
            notes.append(f"{key}: its origin is {repo}, write in [repos]: the session's own repo.")
        elif level == "read":
            notes.append(
                f"{key}: its origin is {repo}, read-only in [repos]: kept read-only (through @reads), not made "
                f"the session's own repo. To let it write: gitgaterules grant {key} write {repo}"
            )
        else:
            notes.append(
                f"{key}: its origin is {repo}, which is not in [repos]: left out, as no conversation can reach "
                f"it today. To make it the session's: gitgaterules grant {key} write {repo}"
            )
        if reads:
            entry["read"] = ["@reads"]
        sessions[key] = entry
    new["sessions"] = sessions
    if not sessions:
        notes.append("No workspace became a session, so the gate will refuse every call until one is added.")
    for name, level in sorted(access.items()):
        if level == "write" and name not in owned:
            notes.append(
                f"{names[name]}: write in [repos] but no workspace's origin, so no session gets it; left out. "
                f"To give it to a session: gitgaterules grant <folder> write {names[name]}"
            )
    if options:
        new["repos"] = options
        notes.append(f"[repos] keeps handover_paths and push_deploys for {', '.join(sorted(options, key=_sort_key))}.")
    notes.append(
        "Each session now exchanges bundles, body files and downloads inside its own folder only, and reaches "
        "only its own repo and its grants."
    )
    return new, notes


def cmd_migrate(args: list[str], say) -> int:
    if args not in ([], ["--write"]):
        return _usage(say)
    path = rules_path()
    loaded = _load(path, say)
    if loaded is None:
        return 1
    text, checked = loaded
    if checked.problems:
        _say_problems(checked, text, say)
        say("Fix these in the v1 file first; nothing was migrated.")
        return 1
    if checked.rules.version == 2:
        say(f"{path} is already a v2 file (it has [sessions]); nothing to migrate.")
        return 0
    new_data, notes = migrate(tomllib.loads(text))
    new = render(new_data)
    if args:
        say("Judgements:")
        for note in notes:
            say(f"- {note}")
        return save(path, text.encode(), new, say)[0]
    say(new, end="")
    say("\nJudgements:")
    for note in notes:
        say(f"- {note}")
    result = check_rules(new, config_dir())
    _say_problems(result, new, say)
    say(f"\nNothing written. To write it, keeping {path.name} as a backup: gitgaterules migrate --write")
    return 1 if result.problems else 0


COMMANDS = {
    "check": cmd_check,
    "show": cmd_show,
    "grant": cmd_grant,
    "revoke": cmd_revoke,
    "add-session": cmd_add_session,
    "remove-session": cmd_remove_session,
    "group": cmd_group,
    "migrate": cmd_migrate,
}


def main(argv: list[str], *, stdout=sys.stdout) -> int:
    def say(text: str = "", end: str = "\n") -> None:
        stdout.write(text + end)
        stdout.flush()

    if not argv:
        say(USAGE, end="")
        return 2
    if argv[0] in ("-h", "--help", "help"):
        say(USAGE, end="")
        return 0
    command = COMMANDS.get(argv[0])
    if command is None:
        say(USAGE, end="")
        return 2
    return command(argv[1:], say)
