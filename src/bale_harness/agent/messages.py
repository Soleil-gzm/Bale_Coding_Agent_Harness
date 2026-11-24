"""Runtime data contracts for the agent loop and tool governance.

These models are intentionally separate from :mod:`bale_harness.contracts`,
which defines the stable Turn/ToolResult entry contracts. Everything here is
JSON serializable and rejects unknown fields so that model/provider output can
never silently inject extra keys into the runtime.
"""

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from bale_harness.contracts import ToolResult

SideEffect = Literal["read", "write", "execute"]
ChatRole = Literal["system", "user", "assistant", "tool"]


class ToolCall(BaseModel):
    """A tool invocation requested by the model."""

    model_config = ConfigDict(extra="forbid")

    call_id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolSpec(BaseModel):
    """Static description of a tool exposed to the model and registry."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    parameters: dict[str, Any]
    side_effect: SideEffect = "read"
    concurrency_safe: bool = False
    timeout_ms: int = Field(default=30_000, ge=1)


class Usage(BaseModel):
    """Token usage reported by a model provider for a single completion."""

    model_config = ConfigDict(extra="forbid")

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)


class ModelResponse(BaseModel):
    """Structured output of a model completion: a final answer or tool calls."""

    model_config = ConfigDict(extra="forbid")

    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: str = "stop"
    usage: Usage = Field(default_factory=Usage)

    @model_validator(mode="after")
    def _require_content_or_tool_calls(self) -> "ModelResponse":
        if self.tool_calls:
            return self
        if self.content is None or not self.content.strip():
            raise ValueError("response must contain non-blank content or tool_calls")
        return self

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


class ChatMessage(BaseModel):
    """A single message in the conversation history."""

    model_config = ConfigDict(extra="forbid")

    role: ChatRole
    content: str
    tool_call_id: str | None = None
    name: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_tool_fields(self) -> "ChatMessage":
        if self.tool_calls and self.role != "assistant":
            raise ValueError("tool_calls are only allowed on assistant messages")
        if self.role == "tool" and not self.tool_call_id:
            raise ValueError("tool messages require tool_call_id")
        return self


class CachedResult(BaseModel):
    """Persisted tool result used to seed the registry on resume."""

    model_config = ConfigDict(extra="forbid")

    call_id: str
    name: str
    args_hash: str
    result: ToolResult


def argument_fingerprint(arguments: dict[str, Any]) -> str:
    """Return a stable hash for tool call arguments."""
    payload = json.dumps(arguments, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
