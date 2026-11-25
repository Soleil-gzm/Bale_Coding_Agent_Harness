from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlparse

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

OPENAI_KEY_REQUIRED = "BALE_OPENAI_API_KEY is required when BALE_PROVIDER=openai"

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def is_local_endpoint(base_url: str) -> bool:
    """Return ``True`` when ``base_url`` points at a loopback host."""
    host = urlparse(base_url).hostname
    return host in LOCAL_HOSTS


class HttpApiKey(BaseModel):
    """One HTTP caller identity backed by a bearer token."""

    model_config = ConfigDict(extra="forbid")

    principal: str = Field(min_length=1)
    scope: Literal["read", "write"] = "write"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BALE_",
        env_file=".env",
        extra="ignore",
    )

    provider: Literal["fake", "openai"] = "fake"
    openai_api_key: SecretStr | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"
    context_window: int = Field(default=128_000, gt=0)
    trace_path: str | None = None
    input_cost_per_1k: float | None = Field(default=None, ge=0)
    output_cost_per_1k: float | None = Field(default=None, ge=0)
    http_api_key: SecretStr | None = None
    http_api_keys: dict[str, HttpApiKey] | None = None
    http_session_db: Path | None = None
    http_rate_limit_per_minute: int = Field(default=0, ge=0)
    http_approvals: bool = False
    http_approval_timeout_seconds: int = Field(default=120, ge=1)
    http_audit_log: Path | None = None
    mcp_config: Path | None = None
    retrieval_enabled: bool = True
    retrieval_top_k: int = Field(default=5, ge=1, le=20)
    sandbox_backend: Literal["local", "docker"] = "local"
    sandbox_image: str = Field(default="python:3.11-slim", min_length=1)
    sandbox_network: bool = False
    sandbox_memory: str = Field(default="1g", min_length=1)
    sandbox_cpus: float = Field(default=1.0, gt=0)
    sandbox_pids_limit: int = Field(default=256, ge=16)
    sandbox_user: str = Field(default="10001:10001", min_length=1)

    @field_validator("http_api_keys")
    @classmethod
    def reject_blank_api_keys(
        cls, value: dict[str, HttpApiKey] | None
    ) -> dict[str, HttpApiKey] | None:
        if value is not None and any(not key.strip() for key in value):
            raise ValueError("HTTP API key names must not be blank")
        return value

    @model_validator(mode="after")
    def validate_provider_credentials(self) -> Self:
        if self.provider != "openai":
            return self
        api_key = self.openai_api_key
        if api_key is not None and api_key.get_secret_value().strip():
            return self
        if is_local_endpoint(self.openai_base_url):
            # A local Ollama server accepts any bearer token.
            return self
        raise ValueError(OPENAI_KEY_REQUIRED)
