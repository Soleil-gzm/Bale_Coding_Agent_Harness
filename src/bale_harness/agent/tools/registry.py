"""Tool protocol, registry, and execution governance.

Every tool call goes through :class:`ToolRegistry`, which centralizes argument
validation, approval, call-id idempotency, per-tool timeout, error
normalization, and the decision of whether a batch of calls may run
concurrently.

Read tools are safe and run without approval. Tools with side effects require
an :class:`~bale_harness.agent.approval.Approver`; without one they are denied.
"""

import asyncio
import time
from typing import Any, Protocol

from bale_harness.agent.approval import Approver
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

    def __init__(self, approver: Approver | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        self._approver = approver
        self._executed: dict[str, ToolResult] = {}

    def register(self, tool: Tool) -> None:
        name = tool.spec.name
        if name in self._tools:
            raise ValueError(f"tool already registered: {name}")
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

        has_side_effects = tool.spec.side_effect != "read"
        if has_side_effects:
            cached = self._executed.get(call.call_id)
            if cached is not None and cached.name == call.name:
                return cached

            if not await self._is_approved(call, tool.spec):
                return ToolResult(
                    call_id=call.call_id,
                    name=call.name,
                    status="denied",
                    content=f"approval denied: {call.name}",
                    error_code="denied",
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
        if has_side_effects:
            self._executed[call.call_id] = result
        return result

    async def _is_approved(self, call: ToolCall, spec: ToolSpec) -> bool:
        if self._approver is None:
            return False
        try:
            return bool(await self._approver.approve(call, spec))
        except Exception:  # fail safe: an approval error means "deny"
            return False

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
