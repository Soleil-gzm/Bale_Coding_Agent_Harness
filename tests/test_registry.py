import asyncio
import time
from typing import Any

import pytest

from bale_harness.agent.approval import PolicyApprover, auto_approve
from bale_harness.agent.messages import ToolCall, ToolSpec
from bale_harness.agent.tools.registry import BaseTool, ToolRegistry
from bale_harness.contracts import ToolResult


class EchoTool(BaseTool):
    spec = ToolSpec(
        name="echo",
        description="echo text",
        parameters={"type": "object"},
        concurrency_safe=True,
    )

    def validate_arguments(self, arguments: dict[str, Any]) -> None:
        if "text" not in arguments:
            raise ValueError("text is required")

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult(
            call_id="",
            name="echo",
            status="success",
            content=str(arguments["text"]),
            latency_ms=0,
        )


class BoomTool(BaseTool):
    spec = ToolSpec(
        name="boom", description="always fails", parameters={}, concurrency_safe=True
    )

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        raise RuntimeError("boom")


class SlowTool(BaseTool):
    spec = ToolSpec(
        name="slow",
        description="sleeps past timeout",
        parameters={},
        concurrency_safe=True,
        timeout_ms=50,
    )

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        await asyncio.sleep(1)
        return ToolResult(
            call_id="", name="slow", status="success", content="done", latency_ms=0
        )


class SleepTool(BaseTool):
    def __init__(self, name: str, concurrency_safe: bool) -> None:
        self.spec = ToolSpec(
            name=name,
            description="sleeps briefly",
            parameters={},
            concurrency_safe=concurrency_safe,
        )

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        await asyncio.sleep(0.1)
        return ToolResult(
            call_id="",
            name=self.spec.name,
            status="success",
            content="ok",
            latency_ms=0,
        )


class WriteTool(BaseTool):
    spec = ToolSpec(
        name="write_file",
        description="writes a file",
        parameters={},
        side_effect="write",
    )

    def __init__(self) -> None:
        self.executions = 0

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        self.executions += 1
        return ToolResult(
            call_id="",
            name="write_file",
            status="success",
            content="written",
            latency_ms=0,
        )


class ExplodingApprover:
    async def approve(self, call: ToolCall, spec: ToolSpec) -> bool:
        raise RuntimeError("approval backend down")


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_register_and_list_specs() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())

    assert [spec.name for spec in registry.list_specs()] == ["echo"]


def test_register_duplicate_name_raises() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())

    with pytest.raises(ValueError, match="already registered"):
        registry.register(EchoTool())


def test_register_write_tool_is_allowed() -> None:
    registry = ToolRegistry()

    registry.register(WriteTool())

    assert [spec.name for spec in registry.list_specs()] == ["write_file"]


def test_write_tool_is_denied_without_approver() -> None:
    registry = ToolRegistry()
    registry.register(WriteTool())

    result = _run(
        registry.execute(ToolCall(call_id="w1", name="write_file", arguments={}))
    )

    assert result.status == "denied"
    assert result.error_code == "denied"
    assert result.call_id == "w1"


def test_write_tool_runs_when_approved() -> None:
    tool = WriteTool()
    registry = ToolRegistry(PolicyApprover("approve_write", auto_approve))
    registry.register(tool)

    result = _run(
        registry.execute(ToolCall(call_id="w1", name="write_file", arguments={}))
    )

    assert result.status == "success"
    assert tool.executions == 1


def test_side_effect_call_is_idempotent_by_call_id() -> None:
    tool = WriteTool()
    registry = ToolRegistry(PolicyApprover("approve_write", auto_approve))
    registry.register(tool)
    call = ToolCall(call_id="w1", name="write_file", arguments={})

    first = _run(registry.execute(call))
    second = _run(registry.execute(call))

    assert first.status == "success"
    assert second.status == "success"
    assert tool.executions == 1


def test_read_tool_skips_approval_even_without_approver() -> None:
    registry = ToolRegistry(PolicyApprover("auto_read"))
    registry.register(EchoTool())

    result = _run(
        registry.execute(ToolCall(call_id="c1", name="echo", arguments={"text": "hi"}))
    )

    assert result.status == "success"


def test_approver_failure_denies_call() -> None:
    registry = ToolRegistry(ExplodingApprover())
    registry.register(WriteTool())

    result = _run(
        registry.execute(ToolCall(call_id="w1", name="write_file", arguments={}))
    )

    assert result.status == "denied"
    assert result.error_code == "denied"


def test_execute_success_propagates_call_id() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())

    result = _run(
        registry.execute(ToolCall(call_id="c1", name="echo", arguments={"text": "hi"}))
    )

    assert result.status == "success"
    assert result.content == "hi"
    assert result.call_id == "c1"


def test_unknown_tool_returns_error() -> None:
    registry = ToolRegistry()

    result = _run(registry.execute(ToolCall(call_id="c1", name="nope", arguments={})))

    assert result.status == "error"
    assert result.error_code == "unknown_tool"
    assert result.call_id == "c1"


def test_invalid_arguments_returns_error() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())

    result = _run(registry.execute(ToolCall(call_id="c1", name="echo", arguments={})))

    assert result.status == "error"
    assert result.error_code == "invalid_arguments"


def test_exception_is_normalized_to_error() -> None:
    registry = ToolRegistry()
    registry.register(BoomTool())

    result = _run(registry.execute(ToolCall(call_id="c1", name="boom", arguments={})))

    assert result.status == "error"
    assert result.error_code == "RuntimeError"
    assert result.call_id == "c1"


def test_timeout_returns_timeout_status() -> None:
    registry = ToolRegistry()
    registry.register(SlowTool())

    result = _run(registry.execute(ToolCall(call_id="c1", name="slow", arguments={})))

    assert result.status == "timeout"
    assert result.error_code == "timeout"


def test_batch_runs_safe_tools_in_parallel() -> None:
    registry = ToolRegistry()
    registry.register(SleepTool("sleep_p", concurrency_safe=True))
    registry.register(SleepTool("sleep_s", concurrency_safe=False))

    parallel_calls = [
        ToolCall(call_id=f"p{i}", name="sleep_p", arguments={}) for i in range(3)
    ]
    serial_calls = [
        ToolCall(call_id=f"s{i}", name="sleep_s", arguments={}) for i in range(3)
    ]

    start = time.perf_counter()
    parallel_results = _run(registry.execute_batch(parallel_calls))
    parallel_seconds = time.perf_counter() - start

    start = time.perf_counter()
    serial_results = _run(registry.execute_batch(serial_calls))
    serial_seconds = time.perf_counter() - start

    assert len(parallel_results) == 3
    assert len(serial_results) == 3
    assert parallel_seconds < serial_seconds


def test_empty_batch_returns_empty() -> None:
    assert _run(ToolRegistry().execute_batch([])) == []
