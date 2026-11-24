import asyncio

from bale_harness.agent.messages import ModelResponse, ToolCall
from bale_harness.providers import FakeProvider, create_provider
from bale_harness.providers_openai import OpenAICompatibleProvider
from bale_harness.settings import Settings


def test_fake_provider_replays_scripted_responses_and_records_calls() -> None:
    responses = [
        ModelResponse(
            tool_calls=[
                ToolCall(call_id="c1", name="read_file", arguments={"path": "a.txt"})
            ]
        ),
        ModelResponse(content="done"),
    ]
    provider = FakeProvider(responses=responses)

    first = asyncio.run(provider.complete([], []))
    second = asyncio.run(provider.complete([], []))

    assert first.has_tool_calls
    assert second.content == "done"
    assert len(provider.calls) == 2


def test_fake_provider_returns_default_when_script_exhausted() -> None:
    provider = FakeProvider(responses=[])

    result = asyncio.run(provider.complete([], []))

    assert result.content == "Task completed."


def test_factory_creates_fake_provider() -> None:
    settings = Settings(_env_file=None, provider="fake")

    assert isinstance(create_provider(settings), FakeProvider)


def test_factory_wires_openai_settings_into_the_provider() -> None:
    secret = "sk-test-sensitive-value"
    settings = Settings(
        _env_file=None,
        provider="openai",
        openai_api_key=secret,
        openai_base_url="https://api.deepseek.com/v1",
        openai_model="deepseek-chat",
    )

    provider = create_provider(settings)

    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider._model == "deepseek-chat"  # noqa: SLF001
    assert provider._base_url == "https://api.deepseek.com/v1"  # noqa: SLF001
    assert provider._api_key == secret  # noqa: SLF001
