import inspect
import json
import re
import subprocess
import sys
from pathlib import Path

import anyio
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent

from conftest import git, make_bundle
from git_gate import Refused, call
from git_gate.gate import Gate
from git_gate.server import INSTRUCTIONS, Relay, build

SRC = Path(__file__).resolve().parents[1] / "src"


TEN = {"clone", "fetch", "push", "prune", "gh", "pr", "issue", "triage", "workflow_run", "label"}


def test_tools_registered(env):
    server = build(env.gate)
    names = {t.name for t in anyio.run(server.list_tools)}
    assert names == TEN


def test_server_tools_equal_call_tools(env):
    """D13: call.TOOLS is the only list of tool names, and the server registers exactly it."""
    registered = [t.name for t in anyio.run(build(env.gate).list_tools)]
    assert len(registered) == len(set(registered))
    assert set(registered) == set(call.TOOLS) == TEN


def test_tool_parameters_match_the_gate_methods(env):
    """A tool schema that drifts from its Gate method would fail only at call time, in the child."""
    for tool in anyio.run(build(env.gate).list_tools):
        params = list(inspect.signature(getattr(Gate, tool.name)).parameters.values())[3:]  # self, rules, token
        schema = tool.input_schema
        assert set(schema["properties"]) == {p.name for p in params}, tool.name
        assert set(schema.get("required", [])) == {p.name for p in params if p.default is p.empty}, tool.name


def test_every_tool_has_a_docstring(env):
    for tool in anyio.run(build(env.gate).list_tools):
        assert len(tool.description or "") > 40, tool.name


def test_dispatch_is_the_only_path():
    """The `getattr(gate, tool)` lookup exists once in src/, in Gate.dispatch."""
    text = "\n".join(p.read_text() for p in SRC.rglob("*.py"))
    assert len(re.findall(r"getattr\(\w+, tool\)", text)) == 1
    assert not re.findall(r"\bgate\.call\(", text)


def test_dispatch_keeps_gate_call_refusals(env):
    with pytest.raises(Refused, match="no tool 'rm'"):
        env.gate.dispatch("rm", {})
    with pytest.raises(Refused, match="no tool"):
        env.gate.dispatch("_load", {})
    with pytest.raises(Refused, match="unexpected keyword"):
        env.gate.dispatch("prune", {"repo": "me/app", "branch": "x", "force": True})
    with pytest.raises(Refused, match="JSON object"):
        env.gate.dispatch("prune", ["me/app"])
    assert "reset --hard" in env.gate.dispatch("clone", {"repo": "me/app", "path": str(env.ws / "c")})


def test_instructions_carry_the_agent_steps():
    for needed in (
        "git bundle create <dir>/.tmp/<name>.bundle <branch> --not --remotes=origin",
        "git fetch --prune <bundle> '+refs/heads/*:refs/remotes/origin/*'",
        "git for-each-ref --format='%(objectname)' refs/remotes/origin",
        "bundle and body files must be *.bundle / *.md|*.txt outside dot directories (except .tmp)",
        "gitgatepush <id>",
        "the user merges",
        "never use SSH",
    ):
        assert needed in INSTRUCTIONS, needed


def test_tool_docs_carry_the_exact_commands(env):
    docs = {t.name: " ".join(t.description.split()) for t in anyio.run(build(env.gate).list_tools)}
    assert "git bundle create <dir>/.tmp/<name>.bundle <branch> --not --remotes=origin" in docs["push"]
    assert "gitgatepush <id>" in docs["push"]
    assert "git fetch --prune <bundle> '+refs/heads/*:refs/remotes/origin/*'" in docs["fetch"]
    assert "git for-each-ref --format='%(objectname)' refs/remotes/origin" in docs["fetch"]
    assert "git -C <path> reset --hard" in docs["clone"]
    for name in ("pr", "issue"):
        assert "*.md" in docs[name] and "outside dot directories" in docs[name]


def test_typed_tool_call_reaches_the_gate(env):
    server = build(env.gate)
    with pytest.raises(ToolError, match="allowlist"):
        anyio.run(server.call_tool, "triage", {"repo": "nobody/x", "number": 1, "add_labels": ["bug"]})
    with pytest.raises(ToolError, match="allowlist"):
        anyio.run(server.call_tool, "workflow_run", {"repo": "nobody/x", "action": "rerun", "run_id": 1})


def test_build_refuses_gate_and_relay_together(env, tmp_path):
    with pytest.raises(ValueError, match="not both"):
        build(env.gate, Relay(tmp_path))


def test_unexpected_exception_becomes_a_utf8_safe_tool_error(env, monkeypatch):
    """No exception text reaches the transport raw: surrogates crash its JSON encoder."""

    def boom(self, rules, token, repo, branch):
        raise RuntimeError(f"bad \udcff name and {token}")

    monkeypatch.setattr(Gate, "prune", boom)
    with pytest.raises(ToolError) as e:
        anyio.run(build(env.gate).call_tool, "prune", {"repo": "me/app", "branch": "x"})
    text = str(e.value)
    text.encode("utf-8")
    assert "RuntimeError" in text and "bad ? name" in text
    assert "TESTTOKEN" not in text and "[token]" in text


def test_unexpected_relay_exception_is_utf8_safe(tmp_path):
    def broken(tool, kwargs):
        raise ValueError("embedded \udcff null")

    with pytest.raises(ToolError) as e:
        anyio.run(build(relay=broken).call_tool, "prune", {"repo": "me/app", "branch": "x"})
    str(e.value).encode("utf-8")
    assert "ValueError" in str(e.value)


def test_call_with_bad_arity_exits_without_serving():
    for argv in (["call"], ["call", "fetch", "extra"]):
        proc = subprocess.run(
            [sys.executable, "-m", "git_gate", *argv],
            input="",
            capture_output=True,
            text=True,
            env={"PYTHONPATH": str(SRC), "PATH": "/usr/bin:/bin"},
            timeout=5,
        )
        assert proc.returncode == 2, argv
        assert "usage" in proc.stderr.lower() and "call <tool>" in proc.stderr
        assert proc.stdout == ""


def test_stdio_round_trip_writes_json(env):
    """One tools/call through the real stdio loop with in-memory streams."""
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "prune", "arguments": {"repo": "nobody/x", "branch": "b"}},
        },
    ]
    server = build(env.gate)
    answered = anyio.Event()
    written: list[str] = []

    class Feed:
        """stdin: the requests, then silence until the answer is out (EOF would stop the server first)."""

        def __init__(self):
            self.lines = [json.dumps(r) + "\n" for r in requests]

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.lines:
                return self.lines.pop(0)
            await answered.wait()
            raise StopAsyncIteration

    class Sink:
        async def write(self, text):
            written.append(text)
            if json.loads(text).get("id") == 2:
                answered.set()

        async def flush(self):
            pass

    async def serve():
        from mcp.server.stdio import stdio_server

        with anyio.fail_after(10):
            async with stdio_server(stdin=Feed(), stdout=Sink()) as (read, write):
                low = server._lowlevel_server
                await low.run(read, write, low.create_initialization_options())

    anyio.run(serve)
    replies = [json.loads(line) for line in written]
    answer = next(r for r in replies if r.get("id") == 2)
    assert answer["result"]["isError"] is True
    assert "allowlist" in answer["result"]["content"][0]["text"]


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
