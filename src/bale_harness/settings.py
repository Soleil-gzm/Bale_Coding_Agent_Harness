from typing import Literal, Self
from urllib.parse import urlparse

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

OPENAI_KEY_REQUIRED = "BALE_OPENAI_API_KEY is required when BALE_PROVIDER=openai"

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def is_local_endpoint(base_url: str) -> bool:
    """Return ``True`` when ``base_url`` points at a loopback host."""
    host = urlparse(base_url).hostname
    return host in LOCAL_HOSTS


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
