from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from bale_harness.contracts import ToolResult, Turn


def make_turn(**overrides: Any) -> Turn:
    values: dict[str, Any] = {
        "message": "Inspect the repository",
        "workspace": Path("workspace"),
        "token_budget": 4096,
    }
    values.update(overrides)
    return Turn(**values)


def make_tool_result(**overrides: Any) -> ToolResult:
    values: dict[str, Any] = {
        "call_id": "call-1",
        "name": "read_file",
        "status": "success",
        "content": "contents",
        "latency_ms": 12,
    }
    values.update(overrides)
    return ToolResult(**values)


def test_turn_defaults_and_generated_ids() -> None:
    turn = make_turn()

    assert isinstance(turn.turn_id, UUID)
    assert isinstance(turn.session_id, UUID)
    assert turn.turn_id != make_turn().turn_id
    assert turn.repo_revision is None
    assert turn.max_iterations == 20
    assert turn.cost_budget is None
    assert turn.approval_mode == "approve_write"
    assert turn.status == "created"


def test_turn_strips_message_without_creating_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "missing"

    turn = make_turn(message="  Inspect this workspace\n", workspace=workspace)

    assert turn.message == "Inspect this workspace"
    assert turn.workspace == workspace
    assert not workspace.exists()


@pytest.mark.parametrize("message", ["", " ", "\n\t"])
def test_turn_rejects_blank_messages(message: str) -> None:
    with pytest.raises(ValidationError):
        make_turn(message=message)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_iterations", 0),
        ("token_budget", 0),
        ("token_budget", -1),
        ("cost_budget", -0.01),
    ],
)
def test_turn_rejects_invalid_budgets(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        make_turn(**{field: value})


def test_turn_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        make_turn(unexpected=True)


def test_turn_json_round_trip() -> None:
    turn = make_turn(repo_revision="abc123", cost_budget=1.25)

    restored = Turn.model_validate_json(turn.model_dump_json())

    assert restored == turn


@pytest.mark.parametrize(
    "status", ["success", "error", "timeout", "denied", "cancelled"]
)
def test_tool_result_supports_all_statuses(status: str) -> None:
    result = make_tool_result(status=status)

    assert result.status == status


def test_tool_result_defaults_and_json_round_trip() -> None:
    result = make_tool_result(metadata={"path": "src/example.py"})

    restored = ToolResult.model_validate_json(result.model_dump_json())

    assert restored == result
    assert result.error_code is None
    assert result.truncated is False


def test_tool_result_rejects_negative_latency_and_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        make_tool_result(latency_ms=-1)

    with pytest.raises(ValidationError, match="extra_forbidden"):
        make_tool_result(unexpected=True)
