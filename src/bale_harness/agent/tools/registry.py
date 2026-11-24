"""Tool protocol, registry, and execution governance.

Every tool call goes through :class:`ToolRegistry`, which centralizes argument
validation, per-tool timeout, error normalization, call-id propagation, and the
decision of whether a batch of calls may run concurrently.

B1 registers read-only tools only. Write/execute tools are rejected at
registration time with a clear error; human approval and write scheduling are
planned for a later milestone.
"""

import asyncio
import time
from typing import Any, Protocol

from bale_harness.agent.messages import ToolCall, ToolSpec
from bale_harness.contracts import ToolResult


class Tool(Protocol):
    """Structural contract for a callable tool."""

    spec: ToolSpec

    def validate_arguments(self, arguments: dict[str, Any]) -> None:
        """Validate arguments; raise ``ValueError`` on invalid input."""

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        """Run the tool and return a normalized result."""


class BaseTool:
    """Convenience base providing a no-op argument validator."""

    spec: ToolSpec

    def validate_arguments(self, arguments: dict[str, Any]) -> None:
        return None


def _elapsed_ms(start: float) -> int:
    return max(0, int((time.perf_counter() - start) * 1000))


class ToolRegistry:
    """Register tools and dispatch calls with governance applied."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        name = tool.spec.name
        if name in self._tools:
            raise ValueError(f"tool already registered: {name}")
        if tool.spec.side_effect != "read":
            raise ValueError(
                f"only read-only tools are supported in B1; "
                f"{name!r} has side_effect {tool.spec.side_effect!r}"
            )
        self._tools[name] = tool

    def list_specs(self) -> list[ToolSpec]:
        return [tool.spec for tool in self._tools.values()]

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    async def execute(self, call: ToolCall) -> ToolResult:
        start = time.perf_counter()
        tool = self._tools.get(call.name)
        if tool is None:
            return ToolResult(
                call_id=call.call_id,
                name=call.name,
                status="error",
                content=f"unknown tool: {call.name}",
                error_code="unknown_tool",
                latency_ms=_elapsed_ms(start),
            )

        try:
            tool.validate_arguments(call.arguments)
        except ValueError as error:
            return ToolResult(
                call_id=call.call_id,
                name=call.name,
                status="error",
                content=str(error),
                error_code="invalid_arguments",
                latency_ms=_elapsed_ms(start),
            )

        timeout_s = tool.spec.timeout_ms / 1000
        try:
            result = await asyncio.wait_for(
                tool.execute(call.arguments), timeout=timeout_s
            )
        except TimeoutError:
            return ToolResult(
                call_id=call.call_id,
                name=call.name,
                status="timeout",
                content=f"tool timed out after {tool.spec.timeout_ms}ms",
                error_code="timeout",
                latency_ms=_elapsed_ms(start),
            )
        except Exception as error:  # normalize every tool failure
            return ToolResult(
                call_id=call.call_id,
                name=call.name,
                status="error",
                content=str(error),
                error_code=type(error).__name__,
                latency_ms=_elapsed_ms(start),
            )

        if result.call_id != call.call_id:
            result = result.model_copy(update={"call_id": call.call_id})
        return result

    def _all_safe_for_parallel(self, calls: list[ToolCall]) -> bool:
        for call in calls:
            tool = self._tools.get(call.name)
            if tool is None:
                return False
            if tool.spec.side_effect != "read" or not tool.spec.concurrency_safe:
                return False
        return True

    async def execute_batch(self, calls: list[ToolCall]) -> list[ToolResult]:
        """Run a batch.

        Calls that are all read-only and concurrency-safe run in parallel;
        any other batch runs sequentially to preserve ordering and safety.
        """
        if not calls:
            return []
        if self._all_safe_for_parallel(calls):
            return await asyncio.gather(*(self.execute(call) for call in calls))
        return [await self.execute(call) for call in calls]
