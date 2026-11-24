"""Markdown run reports built from persisted session state and traces.

A report is a read-only projection: turns come from :class:`SessionStore`,
tool outcomes from persisted calls, and timings/costs from a JSONL trace. It
performs no model calls and no file writes beyond the caller's output path.
"""

from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from bale_harness.agent.messages import CachedResult
from bale_harness.agent.session import TurnRecord
from bale_harness.agent.trace import TraceEvent

_TEXT_LIMIT = 2000


def _clip(text: str, limit: int = _TEXT_LIMIT) -> str:
    """Bound long free-form text so one answer cannot dominate the report."""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n\n… (truncated)"


def _cell(value: str) -> str:
    """Make a value safe for a Markdown table cell."""
    return value.replace("|", "\\|").replace("\n", " ")


def _block(text: str | None) -> list[str]:
    """Render free-form text as a blockquote, or a placeholder when empty."""
    if text is None or not text.strip():
        return ["—"]
    return [f"> {line}" if line else ">" for line in _clip(text).splitlines()]


def _estimated_cost(trace: list[TraceEvent]) -> float | None:
    values = [
        event.data.get("estimated_cost")
        for event in trace
        if event.event == "turn_finished"
    ]
    costs = [value for value in values if isinstance(value, int | float)]
    return float(sum(costs)) if costs else None


def _trace_duration(trace: list[TraceEvent], event_name: str) -> int:
    return sum(
        event.duration_ms or 0
        for event in trace
        if event.event == event_name and event.duration_ms is not None
    )


def build_report(
    *,
    session_id: UUID,
    workspace: Path,
    turns: list[TurnRecord],
    executed: dict[UUID, list[CachedResult]],
    trace: list[TraceEvent] | None = None,
    memory: list[str] | None = None,
    generated_at: datetime | None = None,
) -> str:
    """Render a Markdown report for one session."""
    trace = trace or []
    memory = memory or []
    generated = generated_at or datetime.now(UTC)

    statuses = Counter(turn.status for turn in turns)
    total_input = sum(turn.usage.input_tokens for turn in turns)
    total_output = sum(turn.usage.output_tokens for turn in turns)
    cost = _estimated_cost(trace)

    lines: list[str] = [
        "# Bale Run Report",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Session | `{session_id}` |",
        f"| Workspace | `{workspace}` |",
        f"| Generated | {generated.isoformat()} |",
        f"| Turns | {len(turns)} |",
        f"| Completed | {statuses.get('completed', 0)} |",
        f"| Failed | {statuses.get('failed', 0)} |",
        f"| Cancelled | {statuses.get('cancelled', 0)} |",
        f"| Running | {statuses.get('running', 0)} |",
        f"| Tokens | {total_input} in / {total_output} out"
        f" / {total_input + total_output} total |",
        f"| Estimated cost | {f'{cost:.6f}' if cost is not None else 'n/a'} |",
    ]

    if not turns:
        lines += ["", "_No turns recorded for this session._"]

    for index, turn in enumerate(turns, start=1):
        calls = executed.get(turn.turn_id, [])
        lines += [
            "",
            f"## Turn {index}",
            "",
            "| Field | Value |",
            "|---|---|",
            f"| Turn ID | `{turn.turn_id}` |",
            f"| Status | `{turn.status}` |",
            f"| Iterations | {turn.iteration} |",
            f"| Tool calls | {turn.tool_calls} |",
            f"| Tokens | {turn.usage.input_tokens} in / "
            f"{turn.usage.output_tokens} out / {turn.usage.total_tokens} total |",
            f"| Error | {_cell(turn.error) if turn.error else '—'} |",
            "",
            "**Task**",
            "",
            *_block(turn.message),
            "",
            "**Answer**",
            "",
            *_block(turn.answer),
        ]
        if calls:
            lines += [
                "",
                "**Tool calls**",
                "",
                "| Tool | Status | Error | Latency (ms) |",
                "|---|---|---|---|",
            ]
            lines += [
                f"| {_cell(call.name)} | {call.result.status}"
                f" | {_cell(call.result.error_code or '—')}"
                f" | {call.result.latency_ms} |"
                for call in calls
            ]

    lines += ["", "## Trace", ""]
    if trace:
        counts = Counter(event.event for event in trace)
        lines += ["| Event | Count |", "|---|---|"]
        lines += [f"| {name} | {count} |" for name, count in sorted(counts.items())]
        lines += [
            "",
            f"- Model time: {_trace_duration(trace, 'model_call')} ms",
            f"- Tool time: {_trace_duration(trace, 'tool_call')} ms",
        ]
    else:
        lines.append("_No trace file provided._")

    lines += ["", "## Memory", ""]
    if memory:
        lines += [f"- {_cell(entry)}" for entry in memory]
    else:
        lines.append("_No memory entries._")

    return "\n".join(lines).rstrip() + "\n"
