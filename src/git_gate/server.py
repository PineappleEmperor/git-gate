"""MCP stdio server exposing the gate's tools."""

import json
import subprocess
from collections.abc import Callable
from functools import partial
from pathlib import Path

import anyio.to_thread
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from git_gate import Refused
from git_gate.gate import Gate

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
            result = json.loads(proc.stdout)
        except OSError, ValueError, subprocess.TimeoutExpired:
            raise Refused(f"git-gate release {target.name[:12]} gave no answer.") from None
        if "ok" in result:
            return result["ok"]
        raise Refused(result.get("refused") or result.get("error") or "git-gate returned nothing.")


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
    async def fetch(repo: str, path: str) -> str:
        """Fetch every branch of `repo` into refs/remotes/origin/* of the local repo at `path`."""
        return await run("fetch", repo=repo, path=path)

    @server.tool()
    async def push(repo: str, path: str, branch: str) -> str:
        """Fast-forward push local `branch` from `path` to the same branch on GitHub.

        Refused for protected or default branches, non-fast-forward updates, and commits touching
        .github/workflows/ (the refusal carries a command for the user to run instead).
        """
        return await run("push", repo=repo, path=path, branch=branch)

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
