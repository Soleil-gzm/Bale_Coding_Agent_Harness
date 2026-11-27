"""Structured JSONL tracing for agent runs.

A trace is a sequence of small, append-only events that describes what the
runtime did: when a turn started, which model calls were made, which tools ran
with what status, which checkpoints were captured, and how the turn finished.
It is intentionally redacted: prompts, answers, and tool arguments are never
written, only sizes, names, statuses, and error codes.

Tracing is best-effort. A broken trace file must never change the outcome of a
turn, so exporters swallow their own I/O failures.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from bale_harness.contracts import Turn

SpanType = Literal[
    "turn",
    "model",
    "context",
    "tool",
    "approval",
    "sandbox",
    "checkpoint",
    "retrieval",
    "verifier",
]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def new_span_id() -> str:
    """Return a fresh span identifier."""
    return uuid4().hex


class TraceEvent(BaseModel):
    """One immutable record in a trace file.

    ``span_type``/``span_id``/``parent_id`` turn the flat event list into a
    span tree: every event belongs to at most one span, and child spans point
    at the span that triggered them. Older trace files without these fields
    still validate because every span field is optional.
    """

    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=1)
    ts: str
    event: str
    turn_id: UUID
    session_id: UUID | None = None
    iteration: int | None = None
    name: str | None = None
    status: str | None = None
    duration_ms: int | None = None
    span_type: SpanType | None = None
    trace_id: str | None = None
    span_id: str | None = None
    parent_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class TraceExporter(Protocol):
    """Destination for trace events."""

    async def bind(self, turn: Turn) -> None:
        """Attach the turn whose events are being recorded."""

    async def emit(
        self,
        event: str,
        *,
        iteration: int | None = None,
        name: str | None = None,
        status: str | None = None,
        duration_ms: int | None = None,
        span_type: SpanType | None = None,
        span_id: str | None = None,
        parent_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        """Append one event."""


class SpanSink(Protocol):
    """Minimal span emitter passed to tools and the tool registry."""

    async def emit_span(
        self,
        span_type: SpanType,
        event: str,
        *,
        iteration: int | None = None,
        name: str | None = None,
        status: str | None = None,
        duration_ms: int | None = None,
        span_id: str | None = None,
        parent_id: str | None = None,
        root: bool = False,
        data: dict[str, Any] | None = None,
    ) -> None:
        """Emit one event that belongs to ``span_type``."""


class TraceSpanSink:
    """Stamp spans with a fresh id and a shared parent, then delegate.

    The agent loop binds the turn span as the default parent so approval and
    sandbox spans emitted from deep inside tool execution still point back to
    the turn. Every failure is swallowed: tracing is best-effort.
    """

    def __init__(self, exporter: TraceExporter | None) -> None:
        self._exporter = exporter
        self._default_parent: str | None = None
        self._disabled = False

    def bind_parent(self, span_id: str | None) -> None:
        self._default_parent = span_id

    def disable(self) -> None:
        self._disabled = True

    async def emit_span(
        self,
        span_type: SpanType,
        event: str,
        *,
        iteration: int | None = None,
        name: str | None = None,
        status: str | None = None,
        duration_ms: int | None = None,
        span_id: str | None = None,
        parent_id: str | None = None,
        root: bool = False,
        data: dict[str, Any] | None = None,
    ) -> None:
        if self._exporter is None or self._disabled:
            return
        parent = None if root else (parent_id or self._default_parent)
        try:
            await self._exporter.emit(
                event,
                iteration=iteration,
                name=name,
                status=status,
                duration_ms=duration_ms,
                span_type=span_type,
                span_id=span_id or new_span_id(),
                parent_id=parent,
                data=data,
            )
        except Exception:  # trace failures never change the run outcome
            return


class NullTraceExporter:
    """Exporter that discards every event."""

    async def bind(self, turn: Turn) -> None:
        return None

    async def emit(
        self,
        event: str,
        *,
        iteration: int | None = None,
        name: str | None = None,
        status: str | None = None,
        duration_ms: int | None = None,
        span_type: SpanType | None = None,
        span_id: str | None = None,
        parent_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        return None


class JsonlTraceExporter:
    """Append one JSON object per line to ``path``.

    The file is opened in append mode so a resumed turn can extend the same
    trace. Events are serialized with a lock to preserve ``seq`` ordering.
    When ``max_bytes`` is positive, the file rotates once the next event would
    exceed it, keeping at most ``backups`` numbered files next to it.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        max_bytes: int = 0,
        backups: int = 3,
    ) -> None:
        self.path = Path(path)
        self._max_bytes = max(0, max_bytes)
        self._backups = max(1, backups)
        self._seq = 0
        self._lock = asyncio.Lock()
        self._turn_id: UUID | None = None
        self._session_id: UUID | None = None
        self._trace_id: str | None = None

    async def bind(self, turn: Turn) -> None:
        self._turn_id = turn.turn_id
        self._session_id = turn.session_id
        self._trace_id = new_span_id()

    async def emit(
        self,
        event: str,
        *,
        iteration: int | None = None,
        name: str | None = None,
        status: str | None = None,
        duration_ms: int | None = None,
        span_type: SpanType | None = None,
        span_id: str | None = None,
        parent_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        if self._turn_id is None:
            return
        async with self._lock:
            self._seq += 1
            try:
                record = TraceEvent(
                    seq=self._seq,
                    ts=_utc_now(),
                    event=event,
                    turn_id=self._turn_id,
                    session_id=self._session_id,
                    iteration=iteration,
                    name=name,
                    status=status,
                    duration_ms=duration_ms,
                    span_type=span_type,
                    trace_id=self._trace_id,
                    span_id=span_id,
                    parent_id=parent_id,
                    data=dict(data or {}),
                )
                line = record.model_dump_json() + "\n"
            except Exception:  # unrepresentable event must not break the turn
                return
            try:
                await asyncio.to_thread(self._append, line)
            except Exception:  # trace I/O is best-effort
                return

    def _append(self, line: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self._should_rotate(len(line.encode("utf-8"))):
            self._rotate()
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)

    def _should_rotate(self, incoming: int) -> bool:
        if self._max_bytes <= 0 or not self.path.exists():
            return False
        current = self.path.stat().st_size
        return current > 0 and current + incoming > self._max_bytes

    def _rotate(self) -> None:
        oldest = self._backup_path(self._backups)
        if oldest.exists():
            oldest.unlink()
        for index in range(self._backups - 1, 0, -1):
            source = self._backup_path(index)
            if source.exists():
                source.replace(self._backup_path(index + 1))
        self.path.replace(self._backup_path(1))

    def _backup_path(self, index: int) -> Path:
        return self.path.with_name(f"{self.path.name}.{index}")


class QueueTraceExporter:
    """In-memory exporter that publishes events to an asyncio queue.

    A consumer reads :meth:`stream` while the turn runs; :meth:`close` ends the
    stream after the final event has been delivered.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[TraceEvent | None] = asyncio.Queue()
        self._seq = 0
        self._turn_id: UUID | None = None
        self._session_id: UUID | None = None
        self._trace_id: str | None = None

    async def bind(self, turn: Turn) -> None:
        self._turn_id = turn.turn_id
        self._session_id = turn.session_id
        self._trace_id = new_span_id()

    async def emit(
        self,
        event: str,
        *,
        iteration: int | None = None,
        name: str | None = None,
        status: str | None = None,
        duration_ms: int | None = None,
        span_type: SpanType | None = None,
        span_id: str | None = None,
        parent_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        if self._turn_id is None:
            return
        self._seq += 1
        await self._queue.put(
            TraceEvent(
                seq=self._seq,
                ts=_utc_now(),
                event=event,
                turn_id=self._turn_id,
                session_id=self._session_id,
                iteration=iteration,
                name=name,
                status=status,
                duration_ms=duration_ms,
                span_type=span_type,
                trace_id=self._trace_id,
                span_id=span_id,
                parent_id=parent_id,
                data=dict(data or {}),
            )
        )

    async def close(self) -> None:
        """Signal that no more events will be produced."""
        await self._queue.put(None)

    def close_nowait(self) -> None:
        """Non-blocking variant of :meth:`close` for task callbacks."""
        self._queue.put_nowait(None)

    async def stream(self) -> AsyncIterator[TraceEvent]:
        """Yield events until :meth:`close` is called."""
        while True:
            event = await self._queue.get()
            if event is None:
                return
            yield event


def read_trace(path: str | Path) -> list[TraceEvent]:
    """Read a JSONL trace, skipping blank or malformed lines.

    A missing file yields an empty list so reports can run without a trace.
    """
    file = Path(path)
    if not file.exists():
        return []
    events: list[TraceEvent] = []
    for line in file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            events.append(TraceEvent.model_validate_json(line))
        except Exception:  # partial or corrupt lines are ignored
            continue
    return events


def repeated_tool_calls(events: list[TraceEvent]) -> int:
    """Count tool calls that repeat a tool already invoked in the same run.

    The first call to each tool is free; every later invocation of the same
    tool counts as one repeated call. Non-``tool_call`` events are ignored, so
    this is safe to run over a full trace.
    """
    counts: dict[str, int] = {}
    for event in events:
        if event.event != "tool_call" or event.name is None:
            continue
        counts[event.name] = counts.get(event.name, 0) + 1
    return sum(max(0, count - 1) for count in counts.values())
