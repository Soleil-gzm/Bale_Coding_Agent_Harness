import asyncio
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from bale_harness.agent.messages import ChatMessage, ToolCall, Usage
from bale_harness.agent.session import SessionError, SessionStore
from bale_harness.contracts import ToolResult, Turn


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _turn(workspace: Path, message: str = "do work") -> Turn:
    return Turn(
        message=message,
        workspace=workspace,
        token_budget=4096,
        max_iterations=3,
    )


def _calls() -> list[ToolCall]:
    return [ToolCall(call_id="c1", name="read_file", arguments={"path": "a.txt"})]


def _result() -> ToolResult:
    return ToolResult(
        call_id="c1",
        name="read_file",
        status="success",
        content="ok",
        latency_ms=1,
    )


def _tool_message() -> ChatMessage:
    return ChatMessage(role="tool", content="ok", tool_call_id="c1", name="read_file")


def test_session_round_trip(tmp_path: Path) -> None:
    async def scenario():
        store = SessionStore(tmp_path / "sessions.db")
        turn = _turn(tmp_path)
        await store.start_turn(turn)
        calls = _calls()
        assistant = ChatMessage(role="assistant", content="", tool_calls=calls)
        await store.record_tool_intent(turn.turn_id, assistant, calls)
        await store.complete_iteration(
            turn.turn_id,
            calls,
            [_result()],
            [_tool_message()],
            iteration=1,
            tool_calls=1,
            usage=Usage(total_tokens=9),
        )
        await store.finish_turn(
            turn.turn_id,
            "completed",
            answer="done",
            iterations=1,
            tool_calls=1,
            usage=Usage(total_tokens=9),
        )
        messages = await store.load_messages(turn.session_id)
        executed = await store.load_executed(turn.turn_id)
        record = await store.load_turn(turn.turn_id)
        return turn, messages, executed, record

    turn, messages, executed, record = _run(scenario())

    assert [message.role for message in messages] == ["user", "assistant", "tool"]
    assert messages[1].tool_calls[0].call_id == "c1"
    assert len(executed) == 1
    assert executed[0].result.content == "ok"
    assert record is not None
    assert record.status == "completed"
    assert record.answer == "done"
    assert record.iteration == 1


def test_init_is_idempotent_and_start_turn_is_idempotent(tmp_path: Path) -> None:
    async def scenario():
        store = SessionStore(tmp_path / "sessions.db")
        await store.init()
        await store.init()
        turn = _turn(tmp_path)
        await store.start_turn(turn)
        await store.start_turn(turn)
        return await store.load_messages(turn.session_id)

    messages = _run(scenario())

    assert [message.role for message in messages] == ["user"]


def test_session_rejects_workspace_mismatch(tmp_path: Path) -> None:
    async def scenario():
        store = SessionStore(tmp_path / "sessions.db")
        turn = _turn(tmp_path / "one")
        await store.start_turn(turn)
        await store.ensure_session(turn.session_id, tmp_path / "two")

    with pytest.raises(SessionError, match="belongs to workspace"):
        _run(scenario())


def test_resumable_turn_tracks_running_and_cancelled(tmp_path: Path) -> None:
    async def scenario():
        store = SessionStore(tmp_path / "sessions.db")
        turn = _turn(tmp_path)
        await store.start_turn(turn)
        running = await store.load_resumable_turn(turn.session_id)
        await store.finish_turn(turn.turn_id, "cancelled", error="cancelled")
        cancelled = await store.load_resumable_turn(turn.session_id)
        await store.finish_turn(turn.turn_id, "failed", error="boom")
        failed = await store.load_resumable_turn(turn.session_id)
        return running, cancelled, failed

    running, cancelled, failed = _run(scenario())

    assert running is not None and running.status == "running"
    assert cancelled is not None and cancelled.status == "cancelled"
    assert failed is None


def test_finish_turn_does_not_overwrite_terminal_status(tmp_path: Path) -> None:
    async def scenario():
        store = SessionStore(tmp_path / "sessions.db")
        turn = _turn(tmp_path)
        await store.start_turn(turn)
        await store.finish_turn(turn.turn_id, "completed", answer="done")
        await store.finish_turn(turn.turn_id, "failed", error="late")
        return await store.load_turn(turn.turn_id)

    record = _run(scenario())

    assert record is not None
    assert record.status == "completed"
    assert record.error is None


def test_append_messages_rolls_back_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    turn = _turn(tmp_path)

    async def prepare() -> None:
        calls = _calls() + [ToolCall(call_id="c2", name="read_file")]
        await store.start_turn(turn)
        await store.record_tool_intent(
            turn.turn_id,
            ChatMessage(role="assistant", content="", tool_calls=calls),
            calls,
        )

    _run(prepare())
    before = _run(store.load_messages(turn.session_id))

    original = store._insert_message
    state = {"count": 0}

    def failing_insert(connection, session_id, turn_id, message):
        state["count"] += 1
        if state["count"] == 2:
            raise RuntimeError("disk full")
        return original(connection, session_id, turn_id, message)

    monkeypatch.setattr(store, "_insert_message", failing_insert)

    with pytest.raises(RuntimeError, match="disk full"):
        _run(
            store.complete_iteration(
                turn.turn_id,
                _calls() + [ToolCall(call_id="c2", name="read_file")],
                [_result(), _result().model_copy(update={"call_id": "c2"})],
                [
                    _tool_message(),
                    ChatMessage(
                        role="tool",
                        content="ok",
                        tool_call_id="c2",
                        name="read_file",
                    ),
                ],
                iteration=1,
                tool_calls=2,
                usage=Usage(),
            )
        )

    monkeypatch.setattr(store, "_insert_message", original)
    after = _run(store.load_messages(turn.session_id))

    assert len(after) == len(before)


def test_synthesize_unknown_results_marks_and_pairs(tmp_path: Path) -> None:
    async def scenario():
        store = SessionStore(tmp_path / "sessions.db")
        turn = _turn(tmp_path)
        await store.start_turn(turn)
        await store.record_tool_intent(
            turn.turn_id,
            ChatMessage(role="assistant", content="", tool_calls=_calls()),
            _calls(),
        )
        synthesized = await store.synthesize_unknown_results(turn.turn_id, _calls())
        executed = await store.load_executed(turn.turn_id)
        return synthesized, executed

    synthesized, executed = _run(scenario())

    assert len(synthesized) == 1
    assert synthesized[0].tool_call_id == "c1"
    assert executed[0].result.error_code == "outcome_unknown"


def test_memory_entries_are_returned_in_order(tmp_path: Path) -> None:
    async def scenario():
        store = SessionStore(tmp_path / "sessions.db")
        turn = _turn(tmp_path)
        await store.start_turn(turn)
        await store.append_memory(turn.session_id, turn.turn_id, "one")
        await store.append_memory(turn.session_id, turn.turn_id, "two")
        latest = await store.load_memory(turn.session_id, limit=1)
        all_entries = await store.load_memory(turn.session_id, limit=5)
        return latest, all_entries

    latest, all_entries = _run(scenario())

    assert latest == ["two"]
    assert all_entries == ["one", "two"]


def test_list_turns_returns_insertion_order(tmp_path: Path) -> None:
    async def scenario():
        store = SessionStore(tmp_path / "sessions.db")
        session_id = uuid4()
        first = _turn(tmp_path, "first").model_copy(update={"session_id": session_id})
        second = _turn(tmp_path, "second").model_copy(update={"session_id": session_id})
        await store.start_turn(first)
        await store.start_turn(second)
        await store.finish_turn(first.turn_id, "completed", answer="done")
        return await store.list_turns(session_id)

    turns = _run(scenario())

    assert [turn.message for turn in turns] == ["first", "second"]
    assert [turn.status for turn in turns] == ["completed", "running"]
