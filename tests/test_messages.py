import pytest
from pydantic import ValidationError

from bale_harness.agent.messages import (
    ChatMessage,
    ModelResponse,
    ToolCall,
    ToolSpec,
    Usage,
)


def test_tool_call_defaults_and_rejects_unknown_fields() -> None:
    call = ToolCall(call_id="c1", name="read_file", arguments={"path": "a"})

    assert call.arguments == {"path": "a"}
    assert ToolCall(call_id="c1", name="read").arguments == {}
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ToolCall(call_id="c1", name="read", unexpected=True)


def test_tool_spec_defaults_to_safe_read_only() -> None:
    spec = ToolSpec(name="read_file", description="d", parameters={"type": "object"})

    assert spec.side_effect == "read"
    assert spec.concurrency_safe is False
    assert spec.timeout_ms == 30_000


def test_tool_spec_validates_timeout_and_side_effect() -> None:
    with pytest.raises(ValidationError):
        ToolSpec(name="x", description="d", parameters={}, timeout_ms=0)
    with pytest.raises(ValidationError):
        ToolSpec(name="x", description="d", parameters={}, side_effect="delete")


def test_usage_defaults_zero_and_rejects_negative() -> None:
    assert Usage() == Usage(input_tokens=0, output_tokens=0, total_tokens=0)
    with pytest.raises(ValidationError):
        Usage(input_tokens=-1)


def test_model_response_requires_content_or_tool_calls() -> None:
    assert ModelResponse(content="done").content == "done"
    assert ModelResponse(tool_calls=[ToolCall(call_id="c", name="t")]).has_tool_calls

    with pytest.raises(ValidationError):
        ModelResponse()
    with pytest.raises(ValidationError):
        ModelResponse(content="   ")


def test_model_response_json_round_trip() -> None:
    response = ModelResponse(
        content=None,
        tool_calls=[ToolCall(call_id="c1", name="read_file", arguments={"path": "x"})],
        finish_reason="tool_calls",
        usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15),
    )

    restored = ModelResponse.model_validate_json(response.model_dump_json())

    assert restored == response


@pytest.mark.parametrize("role", ["system", "user", "assistant", "tool"])
def test_chat_message_accepts_all_roles(role: str) -> None:
    msg = ChatMessage(role=role, content="hi", tool_call_id="c1")

    assert ChatMessage.model_validate_json(msg.model_dump_json()) == msg


def test_chat_message_rejects_unknown_role() -> None:
    with pytest.raises(ValidationError):
        ChatMessage(role="bot", content="hi")


def test_tool_calls_are_only_allowed_on_assistant_messages() -> None:
    call = ToolCall(call_id="c1", name="read_file")

    message = ChatMessage(role="assistant", content="", tool_calls=[call])

    assert message.tool_calls == [call]
    with pytest.raises(ValidationError, match="only allowed on assistant"):
        ChatMessage(role="user", content="hi", tool_calls=[call])


def test_tool_message_requires_tool_call_id() -> None:
    with pytest.raises(ValidationError, match="require tool_call_id"):
        ChatMessage(role="tool", content="result")
