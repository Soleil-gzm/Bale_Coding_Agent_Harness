from pathlib import Path

from bale_harness.agent.memory import MEMORY_MAX_CHARS, curate_memory
from bale_harness.contracts import Turn


def _turn(message: str = "fix the bug") -> Turn:
    return Turn(message=message, workspace=Path("ws"), token_budget=4096)


def test_completed_turn_produces_task_and_outcome() -> None:
    entry = curate_memory(
        _turn(), status="completed", answer="patched loop", error=None
    )

    assert entry is not None
    assert "fix the bug" in entry
    assert "patched loop" in entry


def test_non_completed_turn_produces_no_memory() -> None:
    assert curate_memory(_turn(), status="failed", answer=None, error="boom") is None
    assert curate_memory(_turn(), status="cancelled", answer=None, error=None) is None


def test_empty_answer_is_rendered_as_placeholder() -> None:
    entry = curate_memory(_turn(), status="completed", answer="  ", error=None)

    assert entry is not None
    assert "(no answer)" in entry


def test_long_entry_is_truncated() -> None:
    entry = curate_memory(
        _turn("x" * 1000), status="completed", answer="y" * 1000, error=None
    )

    assert entry is not None
    assert len(entry) <= MEMORY_MAX_CHARS
    assert entry.endswith("...")
