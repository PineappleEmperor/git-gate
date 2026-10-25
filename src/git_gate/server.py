"""MCP stdio server exposing the gate's tools."""

import json
import subprocess
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path

import anyio.to_thread
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from git_gate import Refused
from git_gate.config import state_dir
from git_gate.gate import Gate, utf8_safe

RELAY_TIMEOUT = 4 * 3600  # a backstop only; each call enforces the timeouts in rules.toml


class Relay:
    """Runs each call in a fresh process from the release `current` points to when it arrives.

    An installed server lives as long as its conversation; relaying means a new install applies
    to the next call in every open conversation. The tool list itself stays this release's. If
    `current` is rolled back to a release from before the relay, the call runs here instead.
    """

    def __init__(self, root: Path, fallback: Callable[[], Gate] = Gate):
        self.root = root
        self._fallback = fallback
        self._gate: Gate | None = None

    @classmethod
    def for_installed(cls) -> Relay | None:
        release = Path(__file__).resolve().parents[2]
        root = release.parent.parent
        if release.parent.name != "releases" or not (root / "current").is_symlink():
            return None
        return cls(root)

    def __call__(self, tool: str, kwargs: dict) -> str:
        target = (self.root / "current").resolve()
        if not (target / "src/git_gate/call.py").is_file():
            self._gate = self._gate or self._fallback()
            return self._gate.call(tool, getattr(self._gate, tool), **kwargs)
        try:
            proc = subprocess.run(
                [str(target / "bin/git-gate"), "call", tool],
                input=json.dumps(kwargs),
                capture_output=True,
                text=True,
                timeout=RELAY_TIMEOUT,
            )
        except OSError, subprocess.TimeoutExpired:
            raise Refused(f"git-gate release {target.name[:12]} gave no answer.") from None
        result = self._answer(proc.stdout)
        if result is None:
            log = self._log_child_failure(tool, proc)
            where = f"; its stderr and exit code are in {log}" if log else ""
            raise Refused(f"git-gate release {target.name[:12]} gave no answer{where}.")
        # json.loads turns a child's escaped lone surrogate back into one; it must not reach the wire.
        if "ok" in result:
            return utf8_safe(result["ok"])
        raise Refused(utf8_safe(result.get("refused") or result.get("error") or "git-gate returned nothing."))

    @staticmethod
    def _answer(stdout: str) -> dict | None:
        """The child's answer if it is a JSON object whose ok/refused/error values are strings."""
        try:
            result = json.loads(stdout)
        except ValueError:
            return None
        if not isinstance(result, dict):
            return None
        if any(key in result and not isinstance(result[key], str) for key in ("ok", "refused", "error")):
            return None
        return result

    @staticmethod
    def _log_child_failure(tool: str, proc: subprocess.CompletedProcess) -> Path | None:
        """Keep what a child printed on stderr and its exit code; never the token, which this process lacks."""
        log = state_dir() / "errors.log"
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        try:
            log.parent.mkdir(parents=True, exist_ok=True)
            with open(log, "a") as f:
                f.write(f"{stamp} {tool} relay child exited {proc.returncode} without a valid answer\n{proc.stderr}\n")
        except OSError:
            return None
        return log


INSTRUCTIONS = """\
GitHub access for this sandbox. The token lives outside it; use these tools instead of git
remotes or gh auth. `repo` is always owner/name; paths are absolute.

- clone, then `git -C <path> reset --hard` in the sandbox to check out.
- fetch updates origin/* only; merge or rebase locally.
- push takes a branch: fast-forward only, never a protected branch, no tags, no workflow files.
  Run pre-push checks yourself first; the gate runs no hooks.
- prune deletes a remote branch whose PR is merged or closed.
- gh runs the real gh with the repo fixed. Pass args as a list; use --body-file for bodies.
Force-push, tags and workflow changes are the user's: hand them the command the gate returns.
"""


def build(gate: Gate | None = None, relay: Relay | None = None) -> MCPServer:
    if relay is None:
        gate = gate or Gate()
    server = MCPServer("git-gate", instructions=INSTRUCTIONS)

    async def run(tool: str, **kwargs) -> str:
        if relay is not None:
            call = partial(relay, tool, kwargs)
        else:
            call = partial(gate.call, tool, getattr(gate, tool), **kwargs)
        try:
            return await anyio.to_thread.run_sync(call)
        except Refused as e:
            raise ToolError(str(e)) from None

    @server.tool()
    async def clone(repo: str, path: str) -> str:
        """Clone an allowlisted GitHub repo into a new directory under an allowed workspace.

        Nothing is checked out: run `git -C <path> reset --hard` in the sandbox afterwards.
        """
        return await run("clone", repo=repo, path=path)

    @server.tool()
    async def fetch(repo: str, bundle: str, have: list[str] | None = None) -> str:
        """Write every GitHub branch of `repo` to a bundle file at `bundle` (absolute, in a workspace,
        named *.bundle outside dot directories except .tmp, e.g. <dir>/.tmp/fetch.bundle).

        Then, in the sandbox: git fetch --prune <bundle> '+refs/heads/*:refs/remotes/origin/*',
        which makes origin/* exactly GitHub's branches. To leave out history you already hold, pass
        as `have` the output of git for-each-ref --format='%(objectname)' refs/remotes/origin; if
        git then reports missing prerequisites, fetch again with have=[].
        """
        return await run("fetch", repo=repo, bundle=bundle, have=have or [])

    @server.tool()
    async def push(repo: str, branch: str, bundle: str) -> str:
        """Fast-forward push the one branch in `bundle` to the same branch on GitHub.

        Make the bundle in the sandbox: git bundle create <dir>/.tmp/<name>.bundle <branch> --not
        --remotes=origin. Bundle files must be named *.bundle, outside dot directories except .tmp.
        Protected or default branches, non-fast-forward updates and handover paths are refused
        and kept for the user, who runs the `gitgatepush <id>` the refusal names.
        """
        return await run("push", repo=repo, branch=branch, bundle=bundle)

    @server.tool()
    async def prune(repo: str, branch: str) -> str:
        """Delete a remote branch whose pull request is merged or closed and that has not moved since."""
        return await run("prune", repo=repo, branch=branch)

    @server.tool()
    async def gh(repo: str, args: list[str]) -> str:
        """Run `gh <args>` against `repo` (forced; do not pass -R).

        Allowed: pr view/list/diff/checks/create/edit/comment/review/close/reopen/ready,
        issue view/list/create/edit/comment/close/reopen, run list/view/watch/download/rerun/cancel,
        workflow list/view, release list/view/download, label list/create/edit, repo view,
        and api (GET only, endpoints under repos/{owner}/{repo}).
        Body files and download targets must be absolute paths inside a workspace. gh runs outside
        any checkout, so `pr create` needs `--head <branch>`.
        """
        return await run("gh", repo=repo, args=args)

    return server


def main() -> None:
    build(relay=Relay.for_installed()).run("stdio")
