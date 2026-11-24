import asyncio
import json
from pathlib import Path
from uuid import uuid4

from bale_harness.agent.trace import JsonlTraceExporter, NullTraceExporter, TraceEvent
from bale_harness.contracts import Turn


def _turn(workspace: Path) -> Turn:
    return Turn(
        message="do work",
        workspace=workspace,
        session_id=uuid4(),
        turn_id=uuid4(),
        token_budget=4096,
    )


def _read(path: Path) -> list[TraceEvent]:
    return [
        TraceEvent.model_validate(json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def test_jsonl_exporter_writes_ordered_events(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    turn = _turn(tmp_path)

    async def scenario() -> None:
        exporter = JsonlTraceExporter(path)
        await exporter.bind(turn)
        await exporter.emit("turn_started", data={"resumed": False})
        await exporter.emit("model_call", iteration=1, duration_ms=12)
        await exporter.emit("turn_finished", status="completed", duration_ms=20)

    asyncio.run(scenario())

    events = _read(path)
    assert [event.event for event in events] == [
        "turn_started",
        "model_call",
        "turn_finished",
    ]
    assert [event.seq for event in events] == [1, 2, 3]
    assert events[0].turn_id == turn.turn_id
    assert events[0].session_id == turn.session_id
    assert events[1].iteration == 1
    assert events[1].duration_ms == 12
    assert events[2].status == "completed"


def test_jsonl_exporter_appends_and_creates_parent(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "runs" / "trace.jsonl"
    turn = _turn(tmp_path)

    async def scenario() -> None:
        exporter = JsonlTraceExporter(path)
        await exporter.bind(turn)
        await exporter.emit("turn_started")
        await exporter.emit("turn_finished", status="completed")

    asyncio.run(scenario())
    first = path.read_text(encoding="utf-8")

    second_turn = _turn(tmp_path)

    async def resumed() -> None:
        exporter = JsonlTraceExporter(path)
        await exporter.bind(second_turn)
        await exporter.emit("turn_started", data={"resumed": True})

    asyncio.run(resumed())

    assert path.read_text(encoding="utf-8").startswith(first)
    assert len(_read(path)) == 3


def test_jsonl_exporter_writes_utf8(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    turn = _turn(tmp_path)

    async def scenario() -> None:
        exporter = JsonlTraceExporter(path)
        await exporter.bind(turn)
        await exporter.emit("model_call", name="读取文件")

    asyncio.run(scenario())

    assert "读取文件" in path.read_text(encoding="utf-8")


def test_jsonl_exporter_ignores_events_before_bind(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"

    async def scenario() -> None:
        exporter = JsonlTraceExporter(path)
        await exporter.emit("turn_started")

    asyncio.run(scenario())

    assert not path.exists()


def test_jsonl_exporter_swallows_io_errors(tmp_path: Path) -> None:
    directory = tmp_path / "not-a-file"
    directory.mkdir()
    turn = _turn(tmp_path)

    async def scenario() -> None:
        exporter = JsonlTraceExporter(directory)
        await exporter.bind(turn)
        await exporter.emit("turn_started")

    asyncio.run(scenario())

    assert directory.is_dir()


def test_jsonl_exporter_swallows_unserializable_payload(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    turn = _turn(tmp_path)

    async def scenario() -> None:
        exporter = JsonlTraceExporter(path)
        await exporter.bind(turn)
        await exporter.emit("model_call", data={"bad": object()})
        await exporter.emit("turn_finished", status="completed")

    asyncio.run(scenario())

    events = _read(path)
    assert [event.event for event in events] == ["turn_finished"]


def test_null_exporter_writes_nothing(tmp_path: Path) -> None:
    turn = _turn(tmp_path)

    async def scenario() -> None:
        exporter = NullTraceExporter()
        await exporter.bind(turn)
        await exporter.emit("turn_started")
        await exporter.emit("turn_finished", status="completed")

    asyncio.run(scenario())

    assert list(tmp_path.iterdir()) == []
