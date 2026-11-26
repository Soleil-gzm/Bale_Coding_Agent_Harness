"""Tests for the controlled interruption injection used by recovery scenarios."""

import asyncio
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from bale_harness.agent.approval import auto_approve
from bale_harness.agent.loop import build_default_loop
from bale_harness.agent.messages import ModelResponse, ToolCall, ToolSpec
from bale_harness.agent.session import SessionStore
from bale_harness.agent.tools.registry import BaseTool
from bale_harness.contracts import ToolResult, Turn
from bale_harness.eval.interrupt import InterruptController, InterruptSpec
from bale_harness.providers import FakeProvider


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _turn(
    workspace: Path,
    message: str = "do work",
    *,
    session_id: Any = None,
    turn_id: Any = None,
    token_budget: int = 10_000,
    max_iterations: int = 6,
) -> Turn:
    return Turn(
        message=message,
        workspace=workspace,
        session_id=session_id or uuid4(),
        turn_id=turn_id or uuid4(),
        token_budget=token_budget,
        max_iterations=max_iterations,
    )


class CountingWriteTool(BaseTool):
    spec = ToolSpec(
        name="count_write",
        description="counting side-effect write",
        parameters={"type": "object"},
        side_effect="write",
    )

    def __init__(self) -> None:
        self.executions = 0

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        self.executions += 1
        return ToolResult(
            call_id="",
            name="count_write",
            status="success",
            content=f"written {self.executions}",
            latency_ms=0,
        )


class AlwaysWriteProvider:
    """Returns one count_write call on every model turn."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages: list, tools: list) -> ModelResponse:
        self.calls += 1
        return ModelResponse(
            tool_calls=[
                ToolCall(call_id=f"w{self.calls}", name="count_write", arguments={})
            ]
        )


def _write_call(call_id: str) -> ModelResponse:
    return ModelResponse(
        tool_calls=[ToolCall(call_id=call_id, name="count_write", arguments={})]
    )


def test_spec_requires_at_least_one_trigger() -> None:
    with pytest.raises(ValidationError):
        InterruptSpec()


def test_no_hook_runs_to_completion(tmp_path: Path) -> None:
    tool = CountingWriteTool()
    provider = FakeProvider([_write_call("w1"), ModelResponse(content="done")])
    loop = build_default_loop(
        provider, tmp_path, approver=auto_approve, extra_tools=[tool]
    )

    result = _run(loop.run(_turn(tmp_path)))

    assert result.status == "completed"
    assert tool.executions == 1


def test_interrupt_after_iterations(tmp_path: Path) -> None:
    tool = CountingWriteTool()
    controller = InterruptController(InterruptSpec(after_iterations=2))
    provider = FakeProvider([_write_call("w1"), _write_call("w2")])
    loop = build_default_loop(
        provider,
        tmp_path,
        approver=auto_approve,
        extra_tools=[tool],
        interrupt_hook=controller,
    )

    result = _run(loop.run(_turn(tmp_path), cancelled=controller.cancelled))

    assert result.status == "cancelled"
    assert result.iterations == 2
    assert tool.executions == 2
    assert controller.cancelled.is_set()


def test_interrupt_before_side_effect_blocks_execution(tmp_path: Path) -> None:
    tool = CountingWriteTool()
    controller = InterruptController(InterruptSpec(before_side_effect=2))
    provider = AlwaysWriteProvider()
    loop = build_default_loop(
        provider,
        tmp_path,
        approver=auto_approve,
        extra_tools=[tool],
        interrupt_hook=controller,
    )

    result = _run(loop.run(_turn(tmp_path), cancelled=controller.cancelled))

    assert result.status == "cancelled"
    # The second side effect was blocked before execution.
    assert tool.executions == 1
    assert controller.side_effects_seen == 2


def test_interrupt_after_side_effect_not_persisted_then_resume_no_duplicate(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[Any, Any, Any]:
        store = SessionStore(tmp_path / "sessions.db")
        tool = CountingWriteTool()
        controller = InterruptController(InterruptSpec(after_side_effect=1))
        turn = _turn(tmp_path)
        provider = AlwaysWriteProvider()
        loop = build_default_loop(
            provider,
            tmp_path,
            approver=auto_approve,
            session=store,
            extra_tools=[tool],
            interrupt_hook=controller,
        )

        first = await loop.run(turn, cancelled=controller.cancelled)
        stored = await store.load_turn(turn.turn_id)

        resume_provider = FakeProvider([ModelResponse(content="finished")])
        resume_loop = build_default_loop(
            resume_provider,
            tmp_path,
            approver=auto_approve,
            session=store,
            extra_tools=[tool],
        )
        second = await resume_loop.run(turn, resume=True)
        return first, stored, second, tool

    first, stored, second, tool = _run(scenario())

    assert first.status == "cancelled"
    assert stored is not None and stored.status == "cancelled"
    # The interrupted side effect executed once but was not persisted.
    assert tool.executions == 1
    assert second.status == "completed"
    assert second.resumed is True
    # Resuming must not re-execute the side effect.
    assert tool.executions == 1
