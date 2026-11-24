import asyncio
from pathlib import Path

import pytest

from bale_harness.contracts import Turn
from bale_harness.providers import FakeProvider, create_provider
from bale_harness.settings import Settings


def make_turn() -> Turn:
    return Turn(
        message="Inspect the repository",
        workspace=Path("workspace"),
        token_budget=4096,
    )


def test_fake_provider_is_deterministic_and_records_turns() -> None:
    provider = FakeProvider(response="Deterministic response")
    turn = make_turn()

    assert asyncio.run(provider.complete(turn)) == "Deterministic response"
    assert asyncio.run(provider.complete(turn)) == "Deterministic response"
    assert provider.turns == [turn, turn]


def test_factory_creates_fake_provider() -> None:
    settings = Settings(_env_file=None, provider="fake")

    assert isinstance(create_provider(settings), FakeProvider)


def test_factory_does_not_fall_back_to_fake_for_openai() -> None:
    secret = "sk-test-sensitive-value"
    settings = Settings(
        _env_file=None,
        provider="openai",
        openai_api_key=secret,
    )

    with pytest.raises(NotImplementedError, match="not implemented") as error:
        create_provider(settings)

    assert secret not in str(error.value)
