"""ReAct agent loop: the read-only execution runtime for B1.

The loop drives a turn until the model returns a final answer (no tool calls),
a fatal error occurs, the iteration budget is exhausted, or cancellation is
requested. Every tool call goes through the :class:`ToolRegistry`, so
validation, timeout, and error normalization are applied uniformly.
"""

import asyncio
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from bale_harness.agent.context import build_initial_messages
from bale_harness.agent.messages import (
    ChatMessage,
    ModelResponse,
    ToolCall,
    ToolSpec,
    Usage,
)
from bale_harness.agent.security import WorkspaceGuard
from bale_harness.agent.tools import build_default_tools
from bale_harness.agent.tools.registry import ToolRegistry
from bale_harness.contracts import Turn
from bale_harness.providers import ModelProvider


class RunResult(BaseModel):
    """Outcome of executing a turn through the agent loop."""

    model_config = ConfigDict(extra="forbid")

    turn_id: UUID
    status: str
    answer: str | None = None
    iterations: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    usage: Usage = Field(default_factory=Usage)
    error: str | None = None


def assign_call_ids(calls: list[ToolCall]) -> list[ToolCall]:
    """Ensure every call has a non-empty id, preserving existing ones."""
    for call in calls:
        if not call.call_id:
            call.call_id = f"call-{uuid4().hex[:12]}"
    return calls


def _add_usage(total: Usage, delta: Usage) -> Usage:
    return Usage(
        input_tokens=total.input_tokens + delta.input_tokens,
        output_tokens=total.output_tokens + delta.output_tokens,
        total_tokens=total.total_tokens + delta.total_tokens,
    )


class AgentLoop:
    def __init__(self, provider: ModelProvider, registry: ToolRegistry) -> None:
        self._provider = provider
        self._registry = registry

    async def run(
        self, turn: Turn, cancelled: asyncio.Event | None = None
    ) -> RunResult:
        messages = build_initial_messages(turn, self._registry.list_specs())
        total_usage = Usage()
        iterations = 0
        tool_calls = 0

        try:
            for _ in range(turn.max_iterations):
                if cancelled is not None and cancelled.is_set():
                    return RunResult(
                        turn_id=turn.turn_id,
                        status="cancelled",
                        iterations=iterations,
                        tool_calls=tool_calls,
                        usage=total_usage,
                        error="cancelled",
                    )

                iterations += 1
                specs: list[ToolSpec] = self._registry.list_specs()
                response: ModelResponse = await self._provider.complete(messages, specs)
                total_usage = _add_usage(total_usage, response.usage)

                if not response.has_tool_calls:
                    return RunResult(
                        turn_id=turn.turn_id,
                        status="completed",
                        answer=response.content,
                        iterations=iterations,
                        tool_calls=tool_calls,
                        usage=total_usage,
                    )

                calls = assign_call_ids(response.tool_calls)
                results = await self._registry.execute_batch(calls)
                tool_calls += len(calls)

                messages.append(
                    ChatMessage(role="assistant", content=response.content or "")
                )
                for call, result in zip(calls, results, strict=True):
                    messages.append(
                        ChatMessage(
                            role="tool",
                            content=result.content,
                            tool_call_id=call.call_id,
                            name=result.name,
                        )
                    )
                    if result.error_code == "unknown_tool":
                        return RunResult(
                            turn_id=turn.turn_id,
                            status="failed",
                            iterations=iterations,
                            tool_calls=tool_calls,
                            usage=total_usage,
                            error=f"unknown tool: {call.name}",
                        )
        except Exception as error:  # normalize provider/runtime failures
            return RunResult(
                turn_id=turn.turn_id,
                status="failed",
                iterations=iterations,
                tool_calls=tool_calls,
                usage=total_usage,
                error=str(error),
            )

        return RunResult(
            turn_id=turn.turn_id,
            status="failed",
            iterations=iterations,
            tool_calls=tool_calls,
            usage=total_usage,
            error=f"max iterations ({turn.max_iterations}) reached",
        )


def build_default_loop(provider: ModelProvider, workspace: Path) -> AgentLoop:
    """Construct a loop wired to the built-in read-only tools for ``workspace``."""
    guard = WorkspaceGuard(workspace)
    registry = ToolRegistry()
    for tool in build_default_tools(guard):
        registry.register(tool)
    return AgentLoop(provider, registry)
