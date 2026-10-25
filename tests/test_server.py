import anyio
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent

from conftest import git, make_bundle
from git_gate.server import build


def test_tools_registered(env):
    server = build(env.gate)
    names = {t.name for t in anyio.run(server.list_tools)}
    assert names == {"clone", "fetch", "push", "prune", "gh"}


def test_refusal_becomes_tool_error(env):
    server = build(env.gate)
    with pytest.raises(ToolError, match="allowlist"):
        anyio.run(server.call_tool, "fetch", {"repo": "nobody/x", "bundle": str(env.ws / "f.bundle")})


def test_success_returns_text(env):
    server = build(env.gate)
    result = anyio.run(server.call_tool, "clone", {"repo": "me/app", "path": str(env.ws / "c")})
    assert "reset --hard" in str(result)


def test_non_utf8_names_reach_the_agent_as_valid_json(env, checkout):
    """A refusal naming a non-UTF-8 file must serialise; a lone surrogate would kill the stdio server."""
    git("switch", "-q", "-c", "raw", cwd=checkout)
    blob = git("hash-object", "-w", "--stdin", cwd=checkout, input="x\n")
    git("update-index", "--add", "--cacheinfo", f"100644,{blob},.githooks/\udcff", cwd=checkout)
    git("commit", "-q", "-m", "raw", cwd=checkout)
    bundle = make_bundle(checkout, env.ws / "raw.bundle", "raw")
    server = build(env.gate)
    with pytest.raises(ToolError) as e:
        anyio.run(server.call_tool, "push", {"repo": "me/app", "branch": "raw", "bundle": str(bundle)})
    # What MCPServer._handle_call_tool sends back for a ToolError:
    reply = CallToolResult(content=[TextContent(type="text", text=str(e.value))], is_error=True)
    wire = reply.model_dump_json()
    assert "gitgatepush" in wire and ".githooks/?" in wire


def test_non_utf8_success_text_is_valid_json(env):
    result = env.gate.call("fetch", lambda rules, token, **kw: "made \udcff here", repo="me/app")
    assert result == "made ? here"
    TextContent(type="text", text=result).model_dump_json()
