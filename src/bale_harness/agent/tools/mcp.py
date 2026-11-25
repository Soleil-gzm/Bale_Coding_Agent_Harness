"""MCP tool adapter.

MCP tools are discovered from stdio, streamable HTTP, or SSE servers, wrapped
in the same :class:`Tool` protocol as built-in tools, and registered through
:class:`ToolRegistry`, so they share argument validation, approval, timeouts,
and error normalization.

MCP tools default to ``side_effect="write"`` because arbitrary servers may
mutate external state; a server must opt in with ``read_only=true`` to run
without approval.
"""

import json
import re
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any, Literal, Protocol, Self
from urllib.parse import urlparse

from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import CallToolResult
from mcp.types import Tool as McpToolDefinition
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from bale_harness.agent.messages import ToolSpec
from bale_harness.agent.tools.common import (
    MAX_OUTPUT_CHARS,
    error,
    success,
    truncate,
)
from bale_harness.agent.tools.registry import BaseTool
from bale_harness.contracts import ToolResult

DEFAULT_TIMEOUT_MS = 60_000
_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


class McpConfigError(ValueError):
    """Raised when an MCP server configuration file is invalid."""


class McpServerConfig(BaseModel):
    """One MCP server and its tool-governance defaults.

    ``stdio`` servers spawn ``command``. ``http`` (streamable HTTP) and ``sse``
    servers connect to ``url`` with optional ``headers``, for example a bearer
    token for a remote deployment.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=_NAME_PATTERN.pattern)
    transport: Literal["stdio", "http", "sse"] = "stdio"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] | None = None
    cwd: Path | None = None
    url: str | None = None
    headers: dict[str, str] | None = None
    read_only: bool = False
    timeout_ms: int = Field(default=DEFAULT_TIMEOUT_MS, ge=1)

    @model_validator(mode="after")
    def validate_transport_fields(self) -> Self:
        if self.transport == "stdio":
            if not (self.command or "").strip():
                raise ValueError("command is required for stdio MCP servers")
            if self.url is not None or self.headers is not None:
                raise ValueError("url and headers are only valid for http/sse servers")
            return self
        if (
            self.command is not None
            or self.args
            or self.env is not None
            or self.cwd is not None
        ):
            raise ValueError(
                f"command/args/env/cwd are only valid for stdio MCP servers, "
                f"not {self.transport}"
            )
        if not (self.url or "").strip():
            raise ValueError(f"url is required for {self.transport} MCP servers")
        if urlparse(self.url or "").scheme not in {"http", "https"}:
            raise ValueError("url must use http or https")
        return self


class McpConfigFile(BaseModel):
    """Top-level JSON document describing MCP servers."""

    model_config = ConfigDict(extra="forbid")

    servers: list[McpServerConfig] = Field(min_length=1)


class McpSession(Protocol):
    """Minimal session surface required by :class:`McpTool`."""

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> CallToolResult:
        """Invoke one remote tool."""


def _qualified_name(server: str, tool: str) -> str:
    safe_server = re.sub(r"[^A-Za-z0-9_]", "_", server)
    safe_tool = re.sub(r"[^A-Za-z0-9_]", "_", tool)
    return f"mcp_{safe_server}_{safe_tool}"


def _render_content(result: CallToolResult) -> str:
    parts: list[str] = []
    for item in result.content:
        text = getattr(item, "text", None)
        if isinstance(text, str):
            parts.append(text)
            continue
        resource = getattr(item, "resource", None)
        resource_text = getattr(resource, "text", None)
        if isinstance(resource_text, str):
            parts.append(resource_text)
            continue
        data = getattr(item, "data", None)
        if isinstance(data, str):
            parts.append(f"[{item.type} content, {len(data)} chars]")
            continue
        parts.append(item.model_dump_json())
    if not parts and result.structuredContent:
        parts.append(json.dumps(result.structuredContent, ensure_ascii=False))
    return "\n".join(parts)


class McpTool(BaseTool):
    """An MCP tool exposed through the harness tool protocol."""

    def __init__(
        self,
        server: str,
        definition: McpToolDefinition,
        session: McpSession,
        *,
        read_only: bool = False,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> None:
        self._server = server
        self._remote_name = definition.name
        self._session = session
        schema = definition.inputSchema or {"type": "object", "properties": {}}
        self._required = [
            str(key) for key in schema.get("required", []) if isinstance(key, str)
        ]
        self.spec = ToolSpec(
            name=_qualified_name(server, definition.name),
            description=(
                definition.description
                or f"MCP tool {definition.name} from server {server}"
            ),
            parameters=schema,
            side_effect="read" if read_only else "write",
            concurrency_safe=read_only,
            timeout_ms=timeout_ms,
        )

    def validate_arguments(self, arguments: dict[str, Any]) -> None:
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object")
        missing = [key for key in self._required if key not in arguments]
        if missing:
            raise ValueError(f"missing required argument(s): {', '.join(missing)}")

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        start = time.perf_counter()
        try:
            result = await self._session.call_tool(self._remote_name, arguments)
        except Exception as error_value:  # protocol failures become tool errors
            return error(
                self.spec.name,
                f"MCP call failed: {error_value}",
                "mcp_error",
                start,
            )
        content = _render_content(result)
        if getattr(result, "isError", False):
            return error(
                self.spec.name,
                content or "MCP tool returned an error",
                "mcp_tool_error",
                start,
            )
        text, truncated = truncate(content, MAX_OUTPUT_CHARS)
        return success(
            self.spec.name,
            text,
            metadata={
                "server": self._server,
                "tool": self._remote_name,
                "content_items": len(result.content),
            },
            truncated=truncated,
            start=start,
        )


async def _list_tools(session: ClientSession) -> list[McpToolDefinition]:
    definitions: list[McpToolDefinition] = []
    cursor: str | None = None
    while True:
        listed = await session.list_tools(cursor=cursor)
        definitions.extend(listed.tools)
        cursor = listed.nextCursor
        if not cursor:
            return definitions


def _stdio_parameters(config: McpServerConfig) -> StdioServerParameters:
    assert config.command is not None  # guaranteed by config validation
    return StdioServerParameters(
        command=config.command,
        args=list(config.args),
        env=config.env,
        cwd=str(config.cwd) if config.cwd is not None else None,
    )


@asynccontextmanager
async def _open_session(config: McpServerConfig) -> AsyncIterator[ClientSession]:
    """Open an initialized MCP session for the configured transport."""
    if config.transport == "stdio":
        streams = stdio_client(_stdio_parameters(config))
    elif config.transport == "http":
        assert config.url is not None  # guaranteed by config validation
        streams = streamablehttp_client(config.url, headers=config.headers)
    else:
        assert config.url is not None  # guaranteed by config validation
        streams = sse_client(config.url, headers=config.headers)
    async with streams as opened:
        read_stream, write_stream = opened[0], opened[1]
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            yield session


@asynccontextmanager
async def connect_mcp_server(config: McpServerConfig) -> AsyncIterator[list[McpTool]]:
    """Connect one MCP server and yield its adapted tools."""
    async with _open_session(config) as session:
        definitions = await _list_tools(session)
        tools = [
            McpTool(
                config.name,
                definition,
                session,
                read_only=config.read_only,
                timeout_ms=config.timeout_ms,
            )
            for definition in definitions
        ]
        names = [tool.spec.name for tool in tools]
        if len(set(names)) != len(names):
            raise ValueError(f"MCP server {config.name!r} exposes duplicate tool names")
        yield tools


@asynccontextmanager
async def mcp_tools(
    configs: Sequence[McpServerConfig] = (),
) -> AsyncIterator[list[McpTool]]:
    """Connect every configured server and yield the combined tool list."""
    async with AsyncExitStack() as stack:
        tools: list[McpTool] = []
        for config in configs:
            tools.extend(await stack.enter_async_context(connect_mcp_server(config)))
        names = [tool.spec.name for tool in tools]
        if len(set(names)) != len(names):
            raise ValueError("MCP servers expose duplicate qualified tool names")
        yield tools


def load_mcp_config(path: Path) -> list[McpServerConfig]:
    """Load server definitions from JSON, resolving relative ``cwd`` values."""
    path = Path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error_value:
        message = f"cannot read MCP config {path}: {error_value}"
        raise McpConfigError(message) from error_value
    except json.JSONDecodeError as error_value:
        message = f"invalid JSON in {path}: {error_value}"
        raise McpConfigError(message) from error_value

    try:
        document = McpConfigFile.model_validate(payload)
    except ValidationError as error_value:
        message = f"invalid MCP config {path}: {error_value}"
        raise McpConfigError(message) from error_value

    names = [server.name for server in document.servers]
    if len(set(names)) != len(names):
        raise McpConfigError("MCP server names must be unique")

    for server in document.servers:
        if server.cwd is not None and not server.cwd.is_absolute():
            server.cwd = (path.parent / server.cwd).resolve()
    return document.servers
