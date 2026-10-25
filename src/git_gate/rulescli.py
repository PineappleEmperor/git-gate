"""gitgaterules: check, show and edit ~/.config/git-gate/rules.toml from the user's own terminal.

The user runs it outside the sandbox; the gate exposes no tool for it. It reads rules through the
same `check_rules` the gate loads them with, so what it accepts is what the gate accepts.
"""

import re
import sys
import tomllib
from pathlib import Path

from git_gate.config import Checked, Rules, check_rules, config_dir

USAGE = """\
usage: gitgaterules <command>

  check [FILE]   check rules.toml (or FILE) and list every problem
  show           each session's repos, with read or write, groups expanded
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


COMMANDS = {"check": cmd_check, "show": cmd_show}


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
