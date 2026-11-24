import asyncio
from pathlib import Path

from bale_harness.agent.loop import assign_call_ids, build_default_loop
from bale_harness.agent.messages import ModelResponse, ToolCall, Usage
from bale_harness.contracts import Turn
from bale_harness.providers import FakeProvider


def _turn(tmp_path: Path, max_iterations: int = 5) -> Turn:
    return Turn(
        message="read a.txt",
        workspace=tmp_path,
        token_budget=4096,
        max_iterations=max_iterations,
    )


def _run(coro):
    return asyncio.run(coro)


def test_loop_completes_immediately_without_tool_calls(tmp_path: Path) -> None:
    provider = FakeProvider()
    loop = build_default_loop(provider, tmp_path)

    result = _run(loop.run(_turn(tmp_path)))

    assert result.status == "completed"
    assert result.answer == "Task completed."
    assert result.iterations == 1
    assert result.tool_calls == 0


def test_loop_executes_tool_then_completes(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"file contents")
    responses = [
        ModelResponse(
            tool_calls=[
                ToolCall(call_id="c1", name="read_file", arguments={"path": "a.txt"})
            ]
        ),
        ModelResponse(content="the answer"),
    ]
    provider = FakeProvider(responses=responses)
    loop = build_default_loop(provider, tmp_path)

    result = _run(loop.run(_turn(tmp_path)))

    assert result.status == "completed"
    assert result.answer == "the answer"
    assert result.iterations == 2
    assert result.tool_calls == 1
    # Context grows: first call has system+user, second has two extra messages.
    assert len(provider.calls[0][0]) == 2
    assert len(provider.calls[1][0]) == 4


class AlwaysToolProvider:
    def __init__(self, name: str = "read_file") -> None:
        self.name = name

    async def complete(self, messages, tools):
        return ModelResponse(
            tool_calls=[
                ToolCall(call_id="c", name=self.name, arguments={"path": "missing.txt"})
            ]
        )


def test_loop_terminates_when_max_iterations_reached(tmp_path: Path) -> None:
    loop = build_default_loop(AlwaysToolProvider(), tmp_path)

    result = _run(loop.run(_turn(tmp_path, max_iterations=3)))

    assert result.status == "failed"
    assert result.iterations == 3
    assert result.tool_calls == 3
    assert "max iterations" in (result.error or "")


def test_loop_fails_on_unknown_tool(tmp_path: Path) -> None:
    responses = [
        ModelResponse(tool_calls=[ToolCall(call_id="x", name="ghost", arguments={})])
    ]
    loop = build_default_loop(FakeProvider(responses=responses), tmp_path)

    result = _run(loop.run(_turn(tmp_path)))

    assert result.status == "failed"
    assert result.iterations == 1
    assert result.tool_calls == 1
    assert result.error == "unknown tool: ghost"


def test_loop_aggregates_usage(tmp_path: Path) -> None:
    responses = [
        ModelResponse(
            tool_calls=[ToolCall(call_id="c1", name="list_files", arguments={})],
            usage=Usage(input_tokens=5, output_tokens=3, total_tokens=8),
        ),
        ModelResponse(
            content="done",
            usage=Usage(input_tokens=2, output_tokens=4, total_tokens=6),
        ),
    ]
    loop = build_default_loop(FakeProvider(responses=responses), tmp_path)

    result = _run(loop.run(_turn(tmp_path)))

    assert result.status == "completed"
    assert result.usage.input_tokens == 7
    assert result.usage.output_tokens == 7
    assert result.usage.total_tokens == 14


def test_assign_call_ids_fills_missing_and_preserves_existing() -> None:
    calls = [
        ToolCall(call_id="", name="a"),
        ToolCall(call_id="keep", name="b"),
    ]

    assign_call_ids(calls)

    assert calls[0].call_id
    assert calls[0].call_id != "keep"
    assert calls[1].call_id == "keep"


def test_loop_returns_cancelled_when_event_set(tmp_path: Path) -> None:
    loop = build_default_loop(FakeProvider(), tmp_path)
    cancelled = asyncio.Event()
    cancelled.set()

    result = _run(loop.run(_turn(tmp_path), cancelled=cancelled))

    assert result.status == "cancelled"
    assert result.iterations == 0


class ExplodingProvider:
    async def complete(self, messages, tools):
        raise RuntimeError("boom")


def test_loop_normalizes_provider_errors_to_failed(tmp_path: Path) -> None:
    loop = build_default_loop(ExplodingProvider(), tmp_path)

    result = _run(loop.run(_turn(tmp_path)))

    assert result.status == "failed"
    assert result.error == "boom"
