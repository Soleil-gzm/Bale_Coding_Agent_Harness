from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

OPENAI_KEY_REQUIRED = "BALE_OPENAI_API_KEY is required when BALE_PROVIDER=openai"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BALE_",
        env_file=".env",
        extra="ignore",
    )

    provider: Literal["fake", "openai"] = "fake"
    openai_api_key: SecretStr | None = None
    context_window: int = Field(default=128_000, gt=0)
    trace_path: str | None = None
    input_cost_per_1k: float | None = Field(default=None, ge=0)
    output_cost_per_1k: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_provider_credentials(self) -> Self:
        if self.provider == "openai":
            api_key = self.openai_api_key
            if api_key is None or not api_key.get_secret_value().strip():
                raise ValueError(OPENAI_KEY_REQUIRED)
        return self
