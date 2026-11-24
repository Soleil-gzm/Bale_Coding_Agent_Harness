"""Fast after-turn memory curation.

The MVP curator is deterministic and local: it records a bounded summary of a
completed turn. A slower LLM-backed curator can implement the same contract
later without changing the session schema.
"""

from typing import Protocol

from bale_harness.contracts import Turn

MEMORY_MAX_CHARS = 500
TRUNCATION_SUFFIX = "..."


class MemoryCurator(Protocol):
    """Contract for producing an after-turn memory entry."""

    def curate(
        self, turn: Turn, *, answer: str | None, error: str | None
    ) -> str | None:
        """Return a bounded entry, or ``None`` when nothing should be stored."""


class FastMemoryCurator:
    """Deterministic curator that summarizes a completed turn."""

    def __init__(self, max_chars: int = MEMORY_MAX_CHARS) -> None:
        self._max_chars = max_chars

    def curate(
        self, turn: Turn, *, answer: str | None, error: str | None
    ) -> str | None:
        if error is not None and not answer:
            return None
        outcome = (answer or "").strip() or "(no answer)"
        text = f"Task: {turn.message.strip()}\nOutcome: {outcome}"
        if len(text) <= self._max_chars:
            return text
        keep = self._max_chars - len(TRUNCATION_SUFFIX)
        return text[:keep].rstrip() + TRUNCATION_SUFFIX


def curate_memory(
    turn: Turn, *, status: str, answer: str | None, error: str | None
) -> str | None:
    """Return a memory entry for a completed turn, else ``None``."""
    if status != "completed":
        return None
    return FastMemoryCurator().curate(turn, answer=answer, error=error)
