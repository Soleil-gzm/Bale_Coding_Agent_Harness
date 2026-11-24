from pathlib import Path

import pytest

from bale_harness.agent.context import (
    ContextBuilder,
    ContextOverflowError,
    TokenBudget,
    build_initial_messages,
)
from bale_harness.agent.messages import ChatMessage, ToolSpec
from bale_harness.contracts import Turn


def _turn(message: str = "Inspect the repo") -> Turn:
    return Turn(message=message, workspace=Path("ws"), token_budget=4096)


def test_builds_system_then_user_message() -> None:
    messages = build_initial_messages(_turn(), [])

    assert [message.role for message in messages] == ["system", "user"]
    assert messages[-1].content == "Inspect the repo"


def test_tool_descriptions_are_listed_when_tools_present() -> None:
    spec = ToolSpec(name="read_file", description="read a file", parameters={})

    messages = build_initial_messages(_turn(), [spec])

    assert "read_file" in messages[0].content
    assert "read a file" in messages[0].content


def test_no_tools_section_when_tools_empty() -> None:
    messages = build_initial_messages(_turn(), [])

    assert "Available tools" not in messages[0].content


def test_memory_is_included_in_the_stable_prefix() -> None:
    builder = ContextBuilder([], memory=["edited app.py", "tests passed"])

    first = builder.build([ChatMessage(role="user", content="continue")])
    second = builder.build([ChatMessage(role="user", content="continue")])

    assert first[0].content == second[0].content
    assert "edited app.py" in first[0].content
    assert "tests passed" in first[0].content


def test_builder_raises_when_fixed_prefix_exceeds_window() -> None:
    builder = ContextBuilder([], budget=TokenBudget(10, reserved_output=0))

    with pytest.raises(ContextOverflowError):
        builder.build([ChatMessage(role="user", content="hi")])
