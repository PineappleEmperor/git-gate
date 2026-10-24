"""MCP stdio server exposing the gate's tools."""

from functools import partial

import anyio.to_thread
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from git_gate import Refused
from git_gate.gate import Gate

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


def build(gate: Gate | None = None) -> MCPServer:
    gate = gate or Gate()
    server = MCPServer("git-gate", instructions=INSTRUCTIONS)

    async def run(tool: str, **kwargs) -> str:
        try:
            return await anyio.to_thread.run_sync(partial(gate.call, tool, getattr(gate, tool), **kwargs))
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
    build().run("stdio")
