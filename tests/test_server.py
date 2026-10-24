import anyio
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from git_gate.server import build


def test_tools_registered(env):
    server = build(env.gate)
    names = {t.name for t in anyio.run(server.list_tools)}
    assert names == {"clone", "fetch", "push", "prune", "gh"}


def test_refusal_becomes_tool_error(env):
    server = build(env.gate)
    with pytest.raises(ToolError, match="allowlist"):
        anyio.run(server.call_tool, "fetch", {"repo": "nobody/x", "path": str(env.ws)})


def test_success_returns_text(env):
    server = build(env.gate)
    result = anyio.run(server.call_tool, "clone", {"repo": "me/app", "path": str(env.ws / "c")})
    assert "reset --hard" in str(result)
