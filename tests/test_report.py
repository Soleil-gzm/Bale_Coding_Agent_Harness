from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from bale_harness.agent.messages import CachedResult, Usage
from bale_harness.agent.report import build_report
from bale_harness.agent.session import TurnRecord
from bale_harness.agent.trace import TraceEvent
from bale_harness.contracts import ToolResult


def _turn(
    workspace: Path,
    session_id: UUID,
    *,
    message: str = "write docs",
    status: str = "completed",
    answer: str | None = "done",
    error: str | None = None,
) -> TurnRecord:
    return TurnRecord(
        turn_id=uuid4(),
        session_id=session_id,
        message=message,
        workspace=workspace,
        token_budget=1024,
        max_iterations=3,
        approval_mode="approve_write",
        status=status,
        iteration=1,
        tool_calls=1,
        usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15),
        answer=answer,
        error=error,
    )


def _cached_call(name: str = "read_file") -> CachedResult:
    call_id = uuid4().hex[:8]
    return CachedResult(
        call_id=call_id,
        name=name,
        args_hash="hash",
        result=ToolResult(
            call_id=call_id,
            name=name,
            status="success",
            content="ok",
            latency_ms=12,
        ),
    )


def _event(
    seq: int,
    event: str,
    turn_id: UUID,
    session_id: UUID,
    *,
    duration_ms: int | None = None,
    data: dict | None = None,
) -> TraceEvent:
    return TraceEvent(
        seq=seq,
        ts="2026-01-01T00:00:00+00:00",
        event=event,
        turn_id=turn_id,
        session_id=session_id,
        duration_ms=duration_ms,
        data=data or {},
    )


def test_report_summarizes_turns_tools_trace_and_memory(tmp_path: Path) -> None:
    session_id = uuid4()
    done = _turn(tmp_path, session_id)
    failed = _turn(
        tmp_path,
        session_id,
        message="second task",
        status="failed",
        answer=None,
        error="boom",
    )
    trace = [
        _event(1, "turn_started", done.turn_id, session_id),
        _event(2, "model_call", done.turn_id, session_id, duration_ms=120),
        _event(3, "tool_call", done.turn_id, session_id, duration_ms=30),
        _event(
            4,
            "turn_finished",
            done.turn_id,
            session_id,
            duration_ms=200,
            data={"estimated_cost": 0.25},
        ),
    ]

    report = build_report(
        session_id=session_id,
        workspace=tmp_path,
        turns=[done, failed],
        executed={done.turn_id: [_cached_call()]},
        trace=trace,
        memory=["Task: write docs"],
        generated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert report.startswith("# Bale Run Report")
    assert f"| Session | `{session_id}` |" in report
    assert "| Turns | 2 |" in report
    assert "| Completed | 1 |" in report
    assert "| Failed | 1 |" in report
    assert "| Estimated cost | 0.250000 |" in report
    assert "write docs" in report
    assert "second task" in report
    assert "| boom |" in report
    assert "| read_file | success | — | 12 |" in report
    assert "| model_call | 1 |" in report
    assert "| tool_call | 1 |" in report
    assert "Model time: 120 ms" in report
    assert "Tool time: 30 ms" in report
    assert "- Task: write docs" in report
    assert report.endswith("\n")


def test_report_marks_missing_trace_and_memory(tmp_path: Path) -> None:
    session_id = uuid4()
    turn = _turn(tmp_path, session_id)

    report = build_report(
        session_id=session_id,
        workspace=tmp_path,
        turns=[turn],
        executed={},
        generated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert "_No trace file provided._" in report
    assert "_No memory entries._" in report
    assert "| Estimated cost | n/a |" in report


def test_report_handles_empty_session(tmp_path: Path) -> None:
    report = build_report(
        session_id=uuid4(),
        workspace=tmp_path,
        turns=[],
        executed={},
    )

    assert "_No turns recorded for this session._" in report


def test_report_truncates_long_text(tmp_path: Path) -> None:
    session_id = uuid4()
    turn = _turn(tmp_path, session_id, message="x" * 3000)

    report = build_report(
        session_id=session_id,
        workspace=tmp_path,
        turns=[turn],
        executed={},
    )

    assert "… (truncated)" in report
    assert "x" * 3000 not in report
