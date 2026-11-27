import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from bale_harness.agent.approval import auto_approve
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
        "context",
        "model_call",
        "turn_finished",
    ]
    assert events[0]["data"] == {"resumed": False}
    assert events[2]["data"]["usage"]["total_tokens"] == 15
    assert events[2]["data"]["has_tool_calls"] is False
    assert events[3]["status"] == "completed"
    assert events[3]["data"]["estimated_cost"] == pytest.approx(20 / 1000)

    # Spans form a tree below one turn span inside a single trace.
    trace_ids = {event["trace_id"] for event in events}
    assert len(trace_ids) == 1 and None not in trace_ids
    assert [event["span_type"] for event in events] == [
        "turn",
        "context",
        "model",
        "turn",
    ]
    turn_span = events[0]["span_id"]
    assert events[3]["span_id"] == turn_span
    assert events[0]["parent_id"] is None
    assert events[1]["parent_id"] == turn_span
    assert events[2]["parent_id"] == turn_span
    assert all(event["span_id"] for event in events)


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
        "context",
        "model_call",
        "tool_call",
        "context",
        "model_call",
        "turn_finished",
    ]
    tool_event = events[3]
    assert tool_event["name"] == "list_files"
    assert tool_event["status"] == "success"
    assert tool_event["span_type"] == "tool"
    assert tool_event["parent_id"] == events[0]["span_id"]
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


def test_approval_and_sandbox_spans_are_traced(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        call_id="c1",
                        name="shell",
                        arguments={"command": [sys.executable, "-c", "print('ok')"]},
                    )
                ]
            ),
            ModelResponse(content="done"),
        ]
    )
    loop = build_default_loop(
        provider,
        tmp_path,
        approver=auto_approve,
        tracer=JsonlTraceExporter(trace),
    )

    result = asyncio.run(loop.run(_turn(tmp_path)))

    assert result.status == "completed"
    events = _events(trace)
    turn_span = events[0]["span_id"]
    spans = {event["span_type"]: event for event in events if event["span_type"]}
    assert spans["approval"]["status"] == "approved"
    assert spans["approval"]["name"] == "shell"
    assert spans["approval"]["parent_id"] == turn_span
    assert spans["sandbox"]["status"] == "success"
    assert spans["sandbox"]["name"] == "shell"
    assert spans["sandbox"]["parent_id"] == turn_span
    assert spans["sandbox"]["data"]["backend"] == "LocalSandbox"


def test_retrieval_span_records_excerpt_count(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"

    class FakeRetriever:
        def retrieve(self, query: str) -> list[str]:
            return ["excerpt one", "excerpt two"]

    loop = build_default_loop(
        FakeProvider([ModelResponse(content="done")]),
        tmp_path,
        retriever=FakeRetriever(),
        tracer=JsonlTraceExporter(trace),
    )

    result = asyncio.run(loop.run(_turn(tmp_path)))

    assert result.status == "completed"
    events = _events(trace)
    retrieval = next(e for e in events if e["span_type"] == "retrieval")
    assert retrieval["status"] == "success"
    assert retrieval["data"]["excerpts"] == 2
    assert retrieval["parent_id"] == events[0]["span_id"]
