from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

ApprovalMode = Literal["auto_read", "approve_write", "approve_all"]
TurnStatus = Literal[
    "created",
    "running",
    "waiting_approval",
    "completed",
    "failed",
    "cancelled",
]
ToolStatus = Literal["success", "error", "timeout", "denied", "cancelled"]


class Turn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    turn_id: UUID = Field(default_factory=uuid4)
    session_id: UUID = Field(default_factory=uuid4)
    message: str = Field(min_length=1)
    workspace: Path
    repo_revision: str | None = None
    max_iterations: int = Field(default=20, ge=1)
    token_budget: int = Field(gt=0)
    cost_budget: float | None = Field(default=None, ge=0)
    approval_mode: ApprovalMode = "approve_write"
    status: TurnStatus = "created"

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        message = value.strip()
        if not message:
            raise ValueError("message must not be blank")
        return message


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_id: str
    name: str
    status: ToolStatus
    content: str
    error_code: str | None = None
    truncated: bool = False
    latency_ms: int = Field(ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)
