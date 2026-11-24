import pytest
from pydantic import ValidationError

from bale_harness.settings import OPENAI_KEY_REQUIRED, Settings


def test_fake_provider_does_not_require_api_key() -> None:
    settings = Settings(_env_file=None)

    assert settings.provider == "fake"
    assert settings.openai_api_key is None


def test_settings_reads_prefixed_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BALE_PROVIDER", "fake")

    settings = Settings(_env_file=None)

    assert settings.provider == "fake"


@pytest.mark.parametrize("api_key", [None, "", "  \n\t"])
def test_openai_provider_requires_nonblank_api_key(api_key: str | None) -> None:
    with pytest.raises(ValidationError, match=OPENAI_KEY_REQUIRED):
        Settings(_env_file=None, provider="openai", openai_api_key=api_key)


def test_openai_api_key_is_not_exposed_in_repr() -> None:
    secret = "sk-test-sensitive-value"

    settings = Settings(
        _env_file=None,
        provider="openai",
        openai_api_key=secret,
    )

    assert secret not in repr(settings)
    assert secret not in str(settings)
