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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from bale_harness.contracts import Turn


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class TraceEvent(BaseModel):
    """One immutable record in a trace file."""

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
        data: dict[str, Any] | None = None,
    ) -> None:
        """Append one event."""


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
        data: dict[str, Any] | None = None,
    ) -> None:
        return None


class JsonlTraceExporter:
    """Append one JSON object per line to ``path``.

    The file is opened in append mode so a resumed turn can extend the same
    trace. Events are serialized with a lock to preserve ``seq`` ordering.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._seq = 0
        self._lock = asyncio.Lock()
        self._turn_id: UUID | None = None
        self._session_id: UUID | None = None

    async def bind(self, turn: Turn) -> None:
        self._turn_id = turn.turn_id
        self._session_id = turn.session_id

    async def emit(
        self,
        event: str,
        *,
        iteration: int | None = None,
        name: str | None = None,
        status: str | None = None,
        duration_ms: int | None = None,
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
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)


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
