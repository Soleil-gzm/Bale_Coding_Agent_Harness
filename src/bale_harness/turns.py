from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from bale_harness.contracts import ApprovalMode, Turn


class TurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1)
    workspace: Path
    repo_revision: str | None = None
    max_iterations: int = Field(default=20, ge=1)
    token_budget: int = Field(gt=0)
    cost_budget: float | None = Field(default=None, ge=0)
    time_budget: float | None = Field(default=None, gt=0)
    approval_mode: ApprovalMode = "approve_write"

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        message = value.strip()
        if not message:
            raise ValueError("message must not be blank")
        return message


def build_turn(request: TurnRequest) -> Turn:
    return Turn(**request.model_dump(), status="created")
