import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest

from bale_harness.agent.cost import CostRates
from bale_harness.agent.loop import build_default_loop
from bale_harness.agent.messages import (
    ChatMessage,
    ModelResponse,
    ToolCall,
    ToolSpec,
    Usage,
)
from bale_harness.agent.trace import JsonlTraceExporter
from bale_harness.contracts import Turn
from bale_harness.providers import FakeProvider


def _turn(workspace: Path) -> Turn:
    return Turn(
        message="do work",
        workspace=workspace,
        session_id=uuid4(),
        turn_id=uuid4(),
        token_budget=10_000,
    )


def _events(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class BoomProvider:
    async def complete(
        self, messages: list[ChatMessage], tools: list[ToolSpec]
    ) -> ModelResponse:
        raise RuntimeError("boom")


class BrokenTracer:
    async def bind(self, turn: Turn) -> None:
        raise RuntimeError("bind failed")

    async def emit(
        self,
        event: str,
        *,
        iteration: int | None = None,
        name: str | None = None,
        status: str | None = None,
        duration_ms: int | None = None,
        data: dict | None = None,
    ) -> None:
        raise RuntimeError("emit failed")


def test_completed_run_emits_ordered_trace_and_cost(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    provider = FakeProvider(
        [
            ModelResponse(
                content="done",
                usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15),
            )
        ]
    )
    loop = build_default_loop(
        provider,
        tmp_path,
        tracer=JsonlTraceExporter(trace),
        cost_rates=CostRates(input_per_1k=1.0, output_per_1k=2.0),
    )

    result = asyncio.run(loop.run(_turn(tmp_path)))

    assert result.status == "completed"
    assert result.duration_ms >= 0
    assert result.estimated_cost == pytest.approx(20 / 1000)

    events = _events(trace)
    assert [event["event"] for event in events] == [
        "turn_started",
        "model_call",
        "turn_finished",
    ]
    assert events[0]["data"] == {"resumed": False}
    assert events[1]["data"]["usage"]["total_tokens"] == 15
    assert events[1]["data"]["has_tool_calls"] is False
    assert events[2]["status"] == "completed"
    assert events[2]["data"]["estimated_cost"] == pytest.approx(20 / 1000)


def test_tool_calls_are_traced(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    (tmp_path / "readme.md").write_text("# demo", encoding="utf-8")
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=[ToolCall(call_id="c1", name="list_files", arguments={})]
            ),
            ModelResponse(content="done"),
        ]
    )
    loop = build_default_loop(provider, tmp_path, tracer=JsonlTraceExporter(trace))

    result = asyncio.run(loop.run(_turn(tmp_path)))

    assert result.status == "completed"
    events = _events(trace)
    assert [event["event"] for event in events] == [
        "turn_started",
        "model_call",
        "tool_call",
        "model_call",
        "turn_finished",
    ]
    tool_event = events[2]
    assert tool_event["name"] == "list_files"
    assert tool_event["status"] == "success"
    assert tool_event["data"]["call_id"] == "c1"
    assert result.estimated_cost is None


def test_failed_run_records_failure(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    loop = build_default_loop(
        BoomProvider(), tmp_path, tracer=JsonlTraceExporter(trace)
    )

    result = asyncio.run(loop.run(_turn(tmp_path)))

    assert result.status == "failed"
    assert result.error == "boom"
    events = _events(trace)
    assert events[-1]["event"] == "turn_finished"
    assert events[-1]["status"] == "failed"
    assert events[-1]["data"]["error"] == "boom"


def test_cancelled_run_records_failure(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    cancelled = asyncio.Event()
    cancelled.set()
    loop = build_default_loop(
        FakeProvider(), tmp_path, tracer=JsonlTraceExporter(trace)
    )

    result = asyncio.run(loop.run(_turn(tmp_path), cancelled=cancelled))

    assert result.status == "cancelled"
    events = _events(trace)
    assert events[0]["event"] == "turn_started"
    assert events[-1]["status"] == "cancelled"


def test_broken_tracer_does_not_change_the_result(tmp_path: Path) -> None:
    loop = build_default_loop(FakeProvider(), tmp_path, tracer=BrokenTracer())

    result = asyncio.run(loop.run(_turn(tmp_path)))

    assert result.status == "completed"
