from pathlib import Path

from bale_harness.agent.context import build_initial_messages
from bale_harness.agent.messages import ToolSpec
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
