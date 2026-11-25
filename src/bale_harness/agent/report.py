"""Markdown run reports built from persisted session state and traces.

A report is a read-only projection: turns come from :class:`SessionStore`,
tool outcomes from persisted calls, and timings/costs from a JSONL trace. It
performs no model calls and no file writes beyond the caller's output path.

``markdown_to_html`` renders the report subset this module emits (headings,
tables, blockquotes, lists, bold and inline code) as a self-contained page.
"""

import html
import re
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


_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_STYLE = """
body { font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  max-width: 960px; margin: 2rem auto; padding: 0 1rem; color: #1f2328;
  line-height: 1.5; }
h1, h2 { border-bottom: 1px solid #d1d9e0; padding-bottom: .3rem; }
table { border-collapse: collapse; width: 100%; margin: 1rem 0; }
th, td { border: 1px solid #d1d9e0; padding: .4rem .6rem; text-align: left;
  vertical-align: top; }
code { background: #f3f4f6; padding: .1rem .3rem; border-radius: 4px; }
blockquote { margin: .5rem 0; padding: .2rem 1rem; border-left: 4px solid #d1d9e0;
  color: #4b5563; }
"""


def _inline(text: str) -> str:
    """Escape HTML and apply the inline markup used by reports."""
    escaped = html.escape(text, quote=False)
    stash: list[str] = []

    def keep_code(match: re.Match[str]) -> str:
        stash.append(f"<code>{match.group(1)}</code>")
        return f"\x00{len(stash) - 1}\x00"

    escaped = _CODE.sub(keep_code, escaped)
    escaped = _BOLD.sub(r"<strong>\1</strong>", escaped)
    for index, code in enumerate(stash):
        escaped = escaped.replace(f"\x00{index}\x00", code)
    return escaped


def _split_row(row: str) -> list[str]:
    cells = re.split(r"(?<!\\)\|", row.strip().strip("|"))
    return [cell.strip().replace("\\|", "|") for cell in cells]


def _render_table(rows: list[str]) -> str:
    headers = _split_row(rows[0])
    parts = [
        "<table>",
        "<thead><tr>"
        + "".join(f"<th>{_inline(cell)}</th>" for cell in headers)
        + "</tr></thead>",
        "<tbody>",
    ]
    for row in rows[2:]:
        cells = _split_row(row)
        parts.append(
            "<tr>" + "".join(f"<td>{_inline(cell)}</td>" for cell in cells) + "</tr>"
        )
    parts.append("</tbody></table>")
    return "\n".join(parts)


def _is_block_start(line: str) -> bool:
    return line.startswith(("|", "#", ">", "- "))


def _render_body(markdown: str) -> str:
    lines = markdown.splitlines()
    blocks: list[str] = []
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped:
            index += 1
            continue
        if stripped.startswith("|"):
            rows: list[str] = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                rows.append(lines[index].strip())
                index += 1
            blocks.append(
                _render_table(rows) if len(rows) >= 2 else f"<p>{_inline(stripped)}</p>"
            )
            continue
        heading = _HEADING.match(stripped)
        if heading:
            level = len(heading.group(1))
            blocks.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            index += 1
            continue
        if stripped.startswith(">"):
            quotes: list[str] = []
            while index < len(lines) and lines[index].strip().startswith(">"):
                quotes.append(lines[index].strip()[1:].lstrip())
                index += 1
            content = "<br>\n".join(_inline(item) if item else "" for item in quotes)
            blocks.append(f"<blockquote>{content}</blockquote>")
            continue
        if stripped.startswith("- "):
            items: list[str] = []
            while index < len(lines) and lines[index].strip().startswith("- "):
                items.append(f"<li>{_inline(lines[index].strip()[2:])}</li>")
                index += 1
            blocks.append("<ul>\n" + "\n".join(items) + "\n</ul>")
            continue
        paragraph: list[str] = []
        while index < len(lines):
            current = lines[index].strip()
            if not current or _is_block_start(current):
                break
            paragraph.append(current)
            index += 1
        blocks.append(
            "<p>" + "<br>\n".join(_inline(item) for item in paragraph) + "</p>"
        )
    return "\n".join(blocks)


def markdown_to_html(markdown: str, *, title: str = "Bale Run Report") -> str:
    """Render a report as a standalone HTML document without new dependencies."""
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        f"<title>{html.escape(title)}</title>\n<style>{_STYLE}</style>\n"
        f"</head>\n<body>\n{_render_body(markdown)}\n</body>\n</html>\n"
    )
