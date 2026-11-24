"""ReAct agent loop.

The loop drives a turn until the model returns a final answer (no tool calls),
calls the ``finish`` tool successfully, a fatal error occurs, the iteration
budget is exhausted, or cancellation is requested. Every tool call goes through
the :class:`ToolRegistry`, so validation, approval, idempotency, timeout, and
error normalization are applied uniformly.

When a :class:`ShadowGit` checkpoint is attached, each iteration is snapshotted
and the final result carries the accumulated diff.
"""

import asyncio
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from bale_harness.agent.approval import Decider, PolicyApprover
from bale_harness.agent.checkpoint import ShadowGit, ShadowGitError
from bale_harness.agent.context import build_initial_messages
from bale_harness.agent.messages import (
    ChatMessage,
    ModelResponse,
    ToolCall,
    ToolSpec,
    Usage,
)
from bale_harness.agent.sandbox import LocalSandbox, Sandbox
from bale_harness.agent.security import WorkspaceGuard
from bale_harness.agent.tools import build_default_tools
from bale_harness.agent.tools.exec import build_exec_tools
from bale_harness.agent.tools.git import build_git_tools
from bale_harness.agent.tools.registry import ToolRegistry
from bale_harness.agent.tools.write import build_write_tools
from bale_harness.contracts import ApprovalMode, Turn
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
    diff: str | None = None
    checkpoints: list[str] = Field(default_factory=list)


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
    def __init__(
        self,
        provider: ModelProvider,
        registry: ToolRegistry,
        checkpoint: ShadowGit | None = None,
    ) -> None:
        self._provider = provider
        self._registry = registry
        self._checkpoint = checkpoint

    async def run(
        self, turn: Turn, cancelled: asyncio.Event | None = None
    ) -> RunResult:
        messages = build_initial_messages(turn, self._registry.list_specs())
        total_usage = Usage()
        iterations = 0
        tool_calls = 0
        checkpoints: list[str] = []

        try:
            if self._checkpoint is not None:
                await self._checkpoint.init()

            for _ in range(turn.max_iterations):
                if cancelled is not None and cancelled.is_set():
                    return RunResult(
                        turn_id=turn.turn_id,
                        status="cancelled",
                        iterations=iterations,
                        tool_calls=tool_calls,
                        usage=total_usage,
                        error="cancelled",
                        checkpoints=checkpoints,
                    )

                iterations += 1
                specs: list[ToolSpec] = self._registry.list_specs()
                response: ModelResponse = await self._provider.complete(messages, specs)
                total_usage = _add_usage(total_usage, response.usage)

                if not response.has_tool_calls:
                    return await self._completed(
                        turn,
                        response.content,
                        iterations,
                        tool_calls,
                        total_usage,
                        checkpoints,
                    )

                calls = assign_call_ids(response.tool_calls)
                results = await self._registry.execute_batch(calls)
                tool_calls += len(calls)

                if self._checkpoint is not None:
                    snapshot = await self._checkpoint.snapshot(
                        f"bale: iteration {iterations}"
                    )
                    if snapshot is not None:
                        checkpoints.append(snapshot)

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
                            checkpoints=checkpoints,
                        )
                    if call.name == "finish" and result.status == "success":
                        return await self._completed(
                            turn,
                            result.content,
                            iterations,
                            tool_calls,
                            total_usage,
                            checkpoints,
                        )
        except Exception as error:  # normalize provider/runtime failures
            return RunResult(
                turn_id=turn.turn_id,
                status="failed",
                iterations=iterations,
                tool_calls=tool_calls,
                usage=total_usage,
                error=str(error),
                checkpoints=checkpoints,
            )

        return RunResult(
            turn_id=turn.turn_id,
            status="failed",
            iterations=iterations,
            tool_calls=tool_calls,
            usage=total_usage,
            error=f"max iterations ({turn.max_iterations}) reached",
            checkpoints=checkpoints,
        )

    async def _completed(
        self,
        turn: Turn,
        answer: str | None,
        iterations: int,
        tool_calls: int,
        usage: Usage,
        checkpoints: list[str],
    ) -> RunResult:
        return RunResult(
            turn_id=turn.turn_id,
            status="completed",
            answer=answer,
            iterations=iterations,
            tool_calls=tool_calls,
            usage=usage,
            diff=await self._diff(),
            checkpoints=checkpoints,
        )

    async def _diff(self) -> str | None:
        if self._checkpoint is None:
            return None
        try:
            return await self._checkpoint.diff()
        except ShadowGitError:
            return None


def build_default_loop(
    provider: ModelProvider,
    workspace: Path,
    *,
    approval_mode: ApprovalMode = "approve_write",
    approver: Decider | None = None,
    sandbox: Sandbox | None = None,
    checkpoint: ShadowGit | None = None,
) -> AgentLoop:
    """Construct a loop wired to the workspace tools.

    Read tools run freely. Write and execute tools require ``approver`` to
    approve each call; without one they are denied.
    """
    guard = WorkspaceGuard(workspace)
    registry = ToolRegistry(PolicyApprover(approval_mode, approver))
    tools = [
        *build_default_tools(guard),
        *build_write_tools(guard),
        *build_exec_tools(sandbox or LocalSandbox(guard)),
        *build_git_tools(checkpoint),
    ]
    for tool in tools:
        registry.register(tool)
    return AgentLoop(provider, registry, checkpoint=checkpoint)
