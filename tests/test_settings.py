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


def test_context_window_defaults_and_reads_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert Settings(_env_file=None).context_window == 128_000

    monkeypatch.setenv("BALE_CONTEXT_WINDOW", "4096")

    assert Settings(_env_file=None).context_window == 4096


@pytest.mark.parametrize("api_key", [None, "", "  \n\t"])
def test_openai_provider_requires_nonblank_api_key(api_key: str | None) -> None:
    with pytest.raises(ValidationError, match=OPENAI_KEY_REQUIRED):
        Settings(_env_file=None, provider="openai", openai_api_key=api_key)


def test_openai_endpoint_defaults_and_reads_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(_env_file=None)
    assert settings.openai_base_url == "https://api.openai.com/v1"
    assert settings.openai_model == "gpt-4o-mini"

    monkeypatch.setenv("BALE_OPENAI_BASE_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.setenv("BALE_OPENAI_MODEL", "qwen2.5-coder:7b")

    settings = Settings(_env_file=None)
    assert settings.openai_base_url == "http://127.0.0.1:11434/v1"
    assert settings.openai_model == "qwen2.5-coder:7b"


def test_local_openai_endpoint_does_not_require_api_key() -> None:
    settings = Settings(
        _env_file=None,
        provider="openai",
        openai_base_url="http://localhost:11434/v1",
        openai_model="qwen2.5-coder:7b",
    )

    assert settings.openai_api_key is None


def test_remote_openai_endpoint_still_requires_api_key() -> None:
    with pytest.raises(ValidationError, match=OPENAI_KEY_REQUIRED):
        Settings(
            _env_file=None,
            provider="openai",
            openai_base_url="https://api.deepseek.com/v1",
        )


def test_trace_and_cost_settings_default_to_none() -> None:
    settings = Settings(_env_file=None)

    assert settings.trace_path is None
    assert settings.input_cost_per_1k is None
    assert settings.output_cost_per_1k is None


def test_trace_and_cost_settings_read_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BALE_TRACE_PATH", "traces/run.jsonl")
    monkeypatch.setenv("BALE_INPUT_COST_PER_1K", "0.15")
    monkeypatch.setenv("BALE_OUTPUT_COST_PER_1K", "0.6")

    settings = Settings(_env_file=None)

    assert settings.trace_path == "traces/run.jsonl"
    assert settings.input_cost_per_1k == pytest.approx(0.15)
    assert settings.output_cost_per_1k == pytest.approx(0.6)


def test_cost_settings_reject_negative_values() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, input_cost_per_1k=-1)


def test_openai_api_key_is_not_exposed_in_repr() -> None:
    secret = "sk-test-sensitive-value"

    settings = Settings(
        _env_file=None,
        provider="openai",
        openai_api_key=secret,
    )

    assert secret not in repr(settings)
    assert secret not in str(settings)
