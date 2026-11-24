from bale_harness.agent.context import (
    ContextBuilder,
    ContextOverflowError,
    TokenBudget,
)
from bale_harness.agent.history import HistoryTrimmer, estimate_tokens
from bale_harness.agent.messages import ChatMessage, ToolCall


def _tool_round(call_id: str, content: str) -> list[ChatMessage]:
    return [
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[ToolCall(call_id=call_id, name="read_file")],
        ),
        ChatMessage(
            role="tool",
            content=content,
            tool_call_id=call_id,
            name="read_file",
        ),
    ]


def test_estimate_tokens_is_non_negative_and_monotonic() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("a") >= 1
    assert estimate_tokens("a" * 300) > estimate_tokens("a" * 30)


def test_available_history_clamps_to_zero() -> None:
    budget = TokenBudget(100, reserved_output=500)

    available = budget.available_history(
        system_tokens=100, tools_tokens=100, current_tokens=100
    )

    assert available == 0


def test_trim_drops_oldest_whole_turn() -> None:
    history = [
        ChatMessage(role="user", content="first"),
        ChatMessage(role="assistant", content="answer one"),
        ChatMessage(role="user", content="second"),
        ChatMessage(role="assistant", content="answer two"),
    ]

    result = HistoryTrimmer().trim(history, available_tokens=0)

    assert [message.content for message in result.messages] == [
        "second",
        "answer two",
    ]
    assert result.dropped_turns == 1


def test_trim_compacts_oversized_tool_output() -> None:
    trimmer = HistoryTrimmer(tool_char_limit=10)
    messages = [
        ChatMessage(role="user", content="read it"),
        *_tool_round("c1", "x" * 100),
    ]

    result = trimmer.trim(messages, available_tokens=1_000_000)

    tool_message = result.messages[-1]
    assert tool_message.content == "x" * 10 + "\n[truncated]"
    assert result.truncated_tools == 1


def test_pairing_rejects_orphan_tool_message() -> None:
    trimmer = HistoryTrimmer()
    paired = [
        ChatMessage(role="user", content="go"),
        *_tool_round("c1", "ok"),
    ]
    orphan = [
        ChatMessage(role="user", content="go"),
        ChatMessage(role="tool", content="ok", tool_call_id="missing"),
    ]

    assert trimmer.validate_pairing(paired) is True
    assert trimmer.validate_pairing(orphan) is False


def test_builder_prefix_is_stable_and_memory_is_included() -> None:
    builder = ContextBuilder([], memory=["User changed app.py"])

    first = builder.build([ChatMessage(role="user", content="continue")])
    second = builder.build([ChatMessage(role="user", content="continue")])

    assert first[0].role == "system"
    assert first[0].content == second[0].content
    assert "User changed app.py" in first[0].content


def test_builder_drops_old_turns_but_keeps_current_tool_round() -> None:
    old = [
        ChatMessage(role="user", content="old"),
        *_tool_round("c-old", "y" * 2000),
    ]
    current = [
        ChatMessage(role="user", content="new"),
        *_tool_round("c-new", "ok"),
    ]
    builder = ContextBuilder([], budget=TokenBudget(400, reserved_output=0))

    messages = builder.build([*old, *current])

    contents = [message.content for message in messages]
    assert "old" not in contents
    assert "new" in contents
    assert messages[-1].tool_call_id == "c-new"
    assert HistoryTrimmer().validate_pairing(messages[1:]) is True


def test_builder_rejects_when_prefix_exceeds_window() -> None:
    builder = ContextBuilder([], budget=TokenBudget(10, reserved_output=0))

    try:
        builder.build([ChatMessage(role="user", content="hi")])
    except ContextOverflowError as error:
        assert "context window exceeded" in str(error)
    else:  # pragma: no cover - explicit failure path
        raise AssertionError("expected ContextOverflowError")
