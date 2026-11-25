import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.types import CallToolResult, TextContent
from mcp.types import Tool as McpToolDefinition
from pydantic import ValidationError
from typer.testing import CliRunner

from bale_harness.agent.approval import PolicyApprover, auto_approve
from bale_harness.agent.loop import build_default_loop
from bale_harness.agent.messages import ModelResponse, ToolCall
from bale_harness.agent.tools.mcp import (
    McpConfigError,
    McpServerConfig,
    McpTool,
    load_mcp_config,
    mcp_tools,
)
from bale_harness.agent.tools.registry import ToolRegistry
from bale_harness.cli import app
from bale_harness.contracts import Turn
from bale_harness.providers import FakeProvider

runner = CliRunner()


def _definition(name: str = "write_note") -> McpToolDefinition:
    return McpToolDefinition(
        name=name,
        description="Write a note.",
        inputSchema={
            "type": "object",
            "properties": {"content": {"type": "string"}},
            "required": ["content"],
        },
    )


def _text_result(text: str = "ok", *, is_error: bool = False) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        isError=is_error,
    )


class FakeSession:
    def __init__(
        self,
        result: CallToolResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result if result is not None else _text_result()
        self.error = error
        self.calls: list[tuple[str, dict[str, object] | None]] = []

    async def call_tool(
        self, name: str, arguments: dict[str, object] | None = None
    ) -> CallToolResult:
        self.calls.append((name, arguments))
        if self.error is not None:
            raise self.error
        return self.result


def test_mcp_tool_maps_definition_to_spec() -> None:
    tool = McpTool("notes", _definition(), FakeSession())

    assert tool.spec.name == "mcp_notes_write_note"
    assert tool.spec.description == "Write a note."
    assert tool.spec.side_effect == "write"
    assert tool.spec.concurrency_safe is False
    assert tool.spec.parameters["required"] == ["content"]


def test_read_only_mcp_tool_is_parallel_safe() -> None:
    tool = McpTool("notes", _definition(), FakeSession(), read_only=True)

    assert tool.spec.side_effect == "read"
    assert tool.spec.concurrency_safe is True


def test_mcp_tool_validates_required_arguments() -> None:
    tool = McpTool("notes", _definition(), FakeSession())

    tool.validate_arguments({"content": "hello"})
    with pytest.raises(ValueError, match="missing required"):
        tool.validate_arguments({})
    with pytest.raises(ValueError, match="object"):
        tool.validate_arguments(["content"])  # type: ignore[arg-type]


def test_mcp_tool_executes_remote_call() -> None:
    session = FakeSession(_text_result("written"))
    tool = McpTool("notes", _definition(), session)

    result = asyncio.run(tool.execute({"content": "hello"}))

    assert result.status == "success"
    assert result.content == "written"
    assert result.metadata["server"] == "notes"
    assert result.metadata["tool"] == "write_note"
    assert session.calls == [("write_note", {"content": "hello"})]


def test_mcp_tool_uses_structured_content_when_text_is_absent() -> None:
    result_payload = CallToolResult(
        content=[],
        structuredContent={"answer": 42},
    )
    tool = McpTool("notes", _definition(), FakeSession(result_payload))

    result = asyncio.run(tool.execute({"content": "hello"}))

    assert result.status == "success"
    assert '"answer": 42' in result.content


@pytest.mark.parametrize(
    ("session", "error_code"),
    [
        (FakeSession(_text_result("boom", is_error=True)), "mcp_tool_error"),
        (FakeSession(error=RuntimeError("server down")), "mcp_error"),
    ],
)
def test_mcp_tool_maps_failures(session: FakeSession, error_code: str) -> None:
    tool = McpTool("notes", _definition(), session)

    result = asyncio.run(tool.execute({"content": "hello"}))

    assert result.status == "error"
    assert result.error_code == error_code
    assert result.content


def test_registry_requires_approval_for_mcp_write_tool() -> None:
    session = FakeSession()
    tool = McpTool("notes", _definition(), session)
    call = ToolCall(
        call_id="c1",
        name=tool.spec.name,
        arguments={"content": "hello"},
    )

    denied_registry = ToolRegistry()
    denied_registry.register(tool)
    denied = asyncio.run(denied_registry.execute(call))
    assert denied.status == "denied"
    assert session.calls == []

    approved_registry = ToolRegistry(PolicyApprover("approve_write", auto_approve))
    approved_registry.register(tool)
    approved = asyncio.run(approved_registry.execute(call))
    assert approved.status == "success"
    assert session.calls == [("write_note", {"content": "hello"})]


def test_loop_registers_extra_mcp_tool(tmp_path: Path) -> None:
    session = FakeSession(_text_result("note text"))
    tool = McpTool("notes", _definition("read_note"), session, read_only=True)
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        call_id="",
                        name="mcp_notes_read_note",
                        arguments={"content": "hello"},
                    )
                ]
            ),
            ModelResponse(content="done"),
        ]
    )
    loop = build_default_loop(provider, tmp_path, extra_tools=[tool])
    turn = Turn(
        message="read a note",
        workspace=tmp_path,
        token_budget=10_000,
        max_iterations=3,
    )

    result = asyncio.run(loop.run(turn))

    assert result.status == "completed"
    assert result.answer == "done"
    assert session.calls == [("read_note", {"content": "hello"})]


def test_load_mcp_config_resolves_relative_cwd(tmp_path: Path) -> None:
    config_path = tmp_path / "servers.json"
    config_path.write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "name": "notes",
                        "command": sys.executable,
                        "args": ["write_server.py"],
                        "cwd": ".",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    servers = load_mcp_config(config_path)

    assert len(servers) == 1
    assert servers[0].cwd == tmp_path.resolve()
    assert servers[0].timeout_ms == 60_000


def test_load_mcp_config_rejects_invalid_documents(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        json.dumps(
            {
                "servers": [
                    {"name": "same", "command": "python"},
                    {"name": "same", "command": "python"},
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(McpConfigError, match="unique"):
        load_mcp_config(duplicate)

    bad_name = tmp_path / "bad-name.json"
    bad_name.write_text(
        json.dumps({"servers": [{"name": "bad name", "command": "python"}]}),
        encoding="utf-8",
    )
    with pytest.raises(McpConfigError):
        load_mcp_config(bad_name)

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(McpConfigError):
        load_mcp_config(broken)

    with pytest.raises(McpConfigError):
        load_mcp_config(tmp_path / "missing.json")


def test_mcp_config_requires_transport_specific_fields() -> None:
    with pytest.raises(ValidationError, match="command is required"):
        McpServerConfig(name="notes")
    with pytest.raises(ValidationError, match="url is required"):
        McpServerConfig(name="remote", transport="sse")
    with pytest.raises(ValidationError, match="http or https"):
        McpServerConfig(
            name="remote", transport="http", url="ftp://mcp.example.com/mcp"
        )
    with pytest.raises(ValidationError, match="only valid for http/sse"):
        McpServerConfig(name="local", command="python", url="https://example.com")
    with pytest.raises(ValidationError, match="only valid for stdio"):
        McpServerConfig(
            name="remote",
            transport="http",
            url="https://mcp.example.com/mcp",
            command="python",
        )


def test_load_mcp_config_accepts_http_server(tmp_path: Path) -> None:
    config_path = tmp_path / "servers.json"
    config_path.write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "name": "remote",
                        "transport": "http",
                        "url": "https://mcp.example.com/mcp",
                        "headers": {"Authorization": "Bearer token"},
                        "read_only": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    servers = load_mcp_config(config_path)

    assert len(servers) == 1
    assert servers[0].transport == "http"
    assert servers[0].command is None
    assert servers[0].url == "https://mcp.example.com/mcp"
    assert servers[0].headers == {"Authorization": "Bearer token"}


class _FakeClientSession:
    def __init__(self, read_stream: object, write_stream: object) -> None:
        self.read_stream = read_stream
        self.write_stream = write_stream
        self.initialized = False

    async def __aenter__(self) -> "_FakeClientSession":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def initialize(self) -> None:
        self.initialized = True

    async def list_tools(self, cursor: str | None = None) -> SimpleNamespace:
        return SimpleNamespace(tools=[_definition()], nextCursor=None)


def test_http_transport_uses_streamable_http_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: dict[str, object] = {}

    @asynccontextmanager
    async def fake_http(url: str, headers: dict[str, str] | None = None):
        recorded["url"] = url
        recorded["headers"] = headers
        yield ("read", "write", lambda: "session-id")

    monkeypatch.setattr("bale_harness.agent.tools.mcp.streamablehttp_client", fake_http)
    monkeypatch.setattr(
        "bale_harness.agent.tools.mcp.ClientSession", _FakeClientSession
    )
    config = McpServerConfig(
        name="remote",
        transport="http",
        url="https://mcp.example.com/mcp",
        headers={"X-Key": "secret"},
    )

    async def scenario() -> list[str]:
        async with mcp_tools([config]) as tools:
            return [tool.spec.name for tool in tools]

    assert asyncio.run(scenario()) == ["mcp_remote_write_note"]
    assert recorded == {
        "url": "https://mcp.example.com/mcp",
        "headers": {"X-Key": "secret"},
    }


def test_sse_transport_uses_sse_client(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict[str, object] = {}

    @asynccontextmanager
    async def fake_sse(url: str, headers: dict[str, str] | None = None):
        recorded["url"] = url
        recorded["headers"] = headers
        yield ("read", "write")

    monkeypatch.setattr("bale_harness.agent.tools.mcp.sse_client", fake_sse)
    monkeypatch.setattr(
        "bale_harness.agent.tools.mcp.ClientSession", _FakeClientSession
    )
    config = McpServerConfig(
        name="remote", transport="sse", url="https://mcp.example.com/sse"
    )

    async def scenario() -> list[str]:
        async with mcp_tools([config]) as tools:
            return [tool.spec.name for tool in tools]

    assert asyncio.run(scenario()) == ["mcp_remote_write_note"]
    assert recorded == {"url": "https://mcp.example.com/sse", "headers": None}


def test_mcp_tools_without_servers_yields_nothing() -> None:
    async def scenario() -> list[McpTool]:
        async with mcp_tools([]) as tools:
            return tools

    assert asyncio.run(scenario()) == []


def test_stdio_server_round_trip(tmp_path: Path) -> None:
    server = (
        Path(__file__).resolve().parents[1] / "examples" / "mcp" / "write_server.py"
    )
    config = McpServerConfig(
        name="notes",
        command=sys.executable,
        args=[str(server)],
        cwd=tmp_path,
    )

    async def scenario():
        async with mcp_tools([config]) as tools:
            assert [tool.spec.name for tool in tools] == ["mcp_notes_write_file"]
            return await tools[0].execute({"content": "hello mcp"})

    result = asyncio.run(asyncio.wait_for(scenario(), timeout=60))

    assert result.status == "success", result.content
    outputs = list((tmp_path / "output").glob("note_*.txt"))
    assert len(outputs) == 1
    assert outputs[0].read_text(encoding="utf-8") == "hello mcp"


def test_cli_run_connects_mcp_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BALE_PROVIDER", "fake")
    server = (
        Path(__file__).resolve().parents[1] / "examples" / "mcp" / "write_server.py"
    )
    config_path = tmp_path / "servers.json"
    config_path.write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "name": "notes",
                        "command": sys.executable,
                        "args": [str(server)],
                        "cwd": str(tmp_path),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "run",
            "--workspace",
            str(tmp_path),
            "--message",
            "hello",
            "--no-shadow-git",
            "--mcp-config",
            str(config_path),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip().splitlines()[-1])
    assert payload["status"] == "completed"


def test_cli_run_rejects_broken_mcp_config(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "run",
            "--workspace",
            str(tmp_path),
            "--message",
            "hello",
            "--no-shadow-git",
            "--mcp-config",
            str(broken),
        ],
    )

    assert result.exit_code != 0
    assert "invalid" in result.output.lower() or "mcp" in result.output.lower()
