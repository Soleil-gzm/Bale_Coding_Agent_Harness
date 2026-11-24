from typing import Literal, Self

from pydantic import SecretStr, model_validator
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

    @model_validator(mode="after")
    def validate_provider_credentials(self) -> Self:
        if self.provider == "openai":
            api_key = self.openai_api_key
            if api_key is None or not api_key.get_secret_value().strip():
                raise ValueError(OPENAI_KEY_REQUIRED)
        return self
