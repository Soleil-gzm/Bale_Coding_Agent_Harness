"""Shared helpers for built-in tools: timing, result shaping, truncation."""

import time
from typing import Any

from bale_harness.contracts import ToolResult

MAX_OUTPUT_CHARS = 30_000


def elapsed_ms(start: float) -> int:
    return max(0, int((time.perf_counter() - start) * 1000))


def success(
    name: str,
    content: str,
    metadata: dict[str, Any] | None = None,
    truncated: bool = False,
    start: float = 0.0,
) -> ToolResult:
    return ToolResult(
        call_id="",
        name=name,
        status="success",
        content=content,
        truncated=truncated,
        latency_ms=elapsed_ms(start),
        metadata=metadata or {},
    )


def error(name: str, message: str, error_code: str, start: float = 0.0) -> ToolResult:
    return ToolResult(
        call_id="",
        name=name,
        status="error",
        content=message,
        error_code=error_code,
        latency_ms=elapsed_ms(start),
    )


def truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True
