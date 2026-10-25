#!/usr/bin/env python3
"""Summarise git-gate's audit.log: run it from your own terminal, `python3 scripts/audit_report.py`.

Reads the rotated <log>.<YYYYmmddTHHMMSS>[.<n>] files oldest first, then <log> (default
~/.local/state/git-gate/audit.log, or $GIT_GATE_STATE_DIR's).
Lines from releases before session, cwd, reason and ms were recorded count with what they have;
lines that are not JSON objects with a time and a tool are counted and skipped. Standard library only.
"""

import argparse
import datetime
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

OUTCOMES = ("ok", "refused", "handover", "error")
TOP_REASONS = 20
UNRECORDED = "(not recorded)"
ROTATED = re.compile(r"(\d{8}T\d{6})(?:\.(\d+))?")  # the suffix after "audit.log."; fullmatch only


def default_log() -> Path:
    return Path(os.environ.get("GIT_GATE_STATE_DIR", Path.home() / ".local/state/git-gate")) / "audit.log"


def logs(log: Path) -> list[Path]:
    """The rotated files, oldest first, then the log itself."""
    rotated = []
    for path in log.parent.glob(f"{log.name}.*") if log.parent.is_dir() else ():
        m = ROTATED.fullmatch(path.name.removeprefix(log.name + "."))
        if m:
            rotated.append(((m[1], int(m[2] or 0)), path))
    return [path for _, path in sorted(rotated)] + [log]


def read(log: Path) -> tuple[list[dict], int, list[Path]]:
    """The audit lines of every file `logs` names, the number of malformed lines, and the files read."""
    lines, bad, read_from = [], 0, []
    for path in logs(log):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            continue
        read_from.append(path)
        for raw in text.splitlines():
            if not raw.strip():
                continue
            try:
                line = json.loads(raw)
            except ValueError:
                bad += 1
                continue
            if isinstance(line, dict) and isinstance(line.get("time"), str) and isinstance(line.get("tool"), str):
                lines.append(line)
            else:
                bad += 1
    return lines, bad, read_from


def repo_of(line: dict) -> str:
    args = line.get("args")
    repo = args.get("repo") if isinstance(args, dict) else line.get("repo")
    return repo if isinstance(repo, str) else "-"


def text_of(value: object, default: str) -> str:
    return value if isinstance(value, str) and value else default


def table(headers: list[str], rows: list[list]) -> list[str]:
    """Columns two spaces apart: numbers right-aligned, text left-aligned, the last column unpadded."""
    cells = [headers, *[[str(c) for c in r] for r in rows]]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]
    numeric = [all(isinstance(r[i], int) for r in rows) for i in range(len(headers))]
    out = []
    for r in cells:
        parts = [c.rjust(w) if num else c.ljust(w) for c, w, num in zip(r, widths, numeric, strict=True)]
        parts[-1] = r[-1].rjust(widths[-1]) if numeric[-1] else r[-1]
        out.append("  ".join(parts).rstrip())
    return out


def outcome_counts(lines: list[dict]) -> list[int]:
    counts = Counter(line.get("outcome") for line in lines)
    return [len(lines), *(counts[o] for o in OUTCOMES)]


def report(lines: list[dict]) -> list[str]:
    calls = [line for line in lines if line["tool"] != "gitgatepush"]
    pushes = [line for line in lines if line["tool"] == "gitgatepush"]
    out = []

    by_folder = defaultdict(list)
    for line in calls:
        by_folder[text_of(line.get("cwd"), UNRECORDED)].append(line)
    rows = []
    for folder, group in sorted(by_folder.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        sessions = {line["session"] for line in group if isinstance(line.get("session"), str)}
        rows.append([folder, len(sessions), *outcome_counts(group)])
    out += ["Calls per session folder", *table(["folder", "sessions", "calls", *OUTCOMES], rows), ""]

    by_repo_tool = defaultdict(list)
    for line in calls:
        by_repo_tool[repo_of(line), line["tool"]].append(line)
    rows = [
        [repo, tool, *outcome_counts(group)]
        for (repo, tool), group in sorted(by_repo_tool.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    ]
    out += ["Calls per repo and tool", *table(["repo", "tool", "calls", *OUTCOMES], rows), ""]

    reasons = Counter(
        (repo_of(line), line["tool"], line["reason"])
        for line in calls
        if line.get("outcome") != "ok" and isinstance(line.get("reason"), str)
    )
    unrecorded = sum(1 for line in calls if line.get("outcome") != "ok" and not isinstance(line.get("reason"), str))
    top = sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_REASONS]
    rows = [[count, repo, tool, reason] for (repo, tool, reason), count in top]
    out += ["Most common refusal reasons", *table(["count", "repo", "tool", "reason"], rows)]
    if unrecorded:
        out.append(f"({unrecorded} more without a recorded reason)")
    out.append("")

    handed = Counter(repo_of(line) for line in calls if line.get("outcome") == "handover")
    pushed = Counter(repo_of(line) for line in pushes if line.get("outcome") == "pushed")
    failed = Counter(repo_of(line) for line in pushes if line.get("outcome") == "failed")
    rows = [[repo, handed[repo], pushed[repo], failed[repo]] for repo in sorted(handed | pushed | failed)]
    out += ["Handovers", *table(["repo", "handed over", "pushed", "failed"], rows)]
    return out


def since_date(text: str) -> str:
    try:
        return datetime.date.fromisoformat(text).isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a YYYY-MM-DD date") from None


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Summarise git-gate's audit.log (and audit.log.1).")
    parser.add_argument("log", nargs="?", type=Path, default=default_log(), help="default: %(default)s")
    parser.add_argument("--since", type=since_date, metavar="YYYY-MM-DD", help="only lines from this day on")
    args = parser.parse_args(argv)

    lines, bad, read_from = read(args.log)
    if not read_from:
        print(f"no audit log at {args.log}, nor rotated ones beside it")
        return 1
    if args.since:
        lines = [line for line in lines if line["time"][:10] >= args.since]
    days = sorted(line["time"][:10] for line in lines)
    span = f", {days[0]} to {days[-1]}" if days else ""
    skipped = f"; {bad} malformed line(s) skipped" if bad else ""
    rotated = sum(path != args.log for path in read_from)
    print(f"{args.log} and {rotated} rotated file(s): {len(lines)} line(s){span}{skipped}")
    print()
    print("\n".join(report(lines)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
