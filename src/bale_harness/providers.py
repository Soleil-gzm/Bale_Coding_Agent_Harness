from typing import Protocol

from bale_harness.contracts import Turn
from bale_harness.settings import Settings


class ModelProvider(Protocol):
    async def complete(self, turn: Turn) -> str: ...


class FakeProvider:
    def __init__(self, response: str = "Fake response") -> None:
        self.response = response
        self.turns: list[Turn] = []

    async def complete(self, turn: Turn) -> str:
        self.turns.append(turn)
        return self.response


def create_provider(settings: Settings) -> ModelProvider:
    if settings.provider == "fake":
        return FakeProvider()
    raise NotImplementedError("OpenAI provider is not implemented")
