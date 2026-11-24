from collections import deque
from typing import Protocol

from bale_harness.agent.messages import ChatMessage, ModelResponse, ToolSpec
from bale_harness.providers_openai import OpenAICompatibleProvider
from bale_harness.settings import Settings


class ModelProvider(Protocol):
    """Structured model completion contract for the agent loop."""

    async def complete(
        self, messages: list[ChatMessage], tools: list[ToolSpec]
    ) -> ModelResponse:
        """Return a final answer or the tool calls the model wants to make."""


class FakeProvider:
    """Offline provider that replays scripted responses for deterministic tests."""

    def __init__(
        self,
        responses: list[ModelResponse] | None = None,
        default: ModelResponse | None = None,
    ) -> None:
        self._responses: deque[ModelResponse] = deque(responses or [])
        self._default = default or ModelResponse(content="Task completed.")
        self.calls: list[tuple[list[ChatMessage], list[ToolSpec]]] = []

    async def complete(
        self, messages: list[ChatMessage], tools: list[ToolSpec]
    ) -> ModelResponse:
        self.calls.append((list(messages), tools))
        if self._responses:
            return self._responses.popleft()
        return self._default


def create_provider(settings: Settings) -> ModelProvider:
    """Build the provider selected by ``BALE_PROVIDER``."""
    if settings.provider == "fake":
        return FakeProvider()
    api_key = settings.openai_api_key
    return OpenAICompatibleProvider(
        api_key=api_key.get_secret_value() if api_key is not None else "",
        model=settings.openai_model,
        base_url=settings.openai_base_url,
    )
