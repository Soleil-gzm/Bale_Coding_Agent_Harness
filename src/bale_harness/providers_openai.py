"""OpenAI-compatible Chat Completions provider.

Works with any endpoint that implements ``POST /chat/completions``, including
OpenAI, DeepSeek, and a local Ollama server (``/v1`` compatibility mode).
"""

import asyncio
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from bale_harness.agent.messages import (
    ChatMessage,
    ModelResponse,
    ToolCall,
    ToolSpec,
    Usage,
)

Transport = Callable[[dict[str, Any], float], dict[str, Any]]

_BACKOFF_BASE_S = 0.2


class OpenAIProviderError(RuntimeError):
    """Raised when an OpenAI-compatible endpoint cannot produce a response."""


class OpenAICompatibleProvider:
    """Provider that talks to an OpenAI-compatible Chat Completions API."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        timeout_s: float = 60.0,
        max_retries: int = 2,
        transport: Transport | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._max_retries = max_retries
        self._transport: Transport = transport or self._urllib_transport

    async def complete(
        self, messages: list[ChatMessage], tools: list[ToolSpec]
    ) -> ModelResponse:
        """Return the model's final answer or requested tool calls."""
        payload = self._build_payload(messages, tools)
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                data = await asyncio.to_thread(
                    self._transport, payload, self._timeout_s
                )
                return self._parse_response(data)
            except urllib.error.HTTPError as exc:
                if exc.code != 429 and exc.code < 500:
                    raise OpenAIProviderError(
                        self._redact(f"HTTP {exc.code}: {exc.reason}")
                    ) from exc
                last_error = exc
            except urllib.error.URLError as exc:
                last_error = exc
            except TimeoutError:
                # Read/connect timeouts are transient; retry them and surface a
                # provider error so callers do not mistake them for budget stops.
                last_error = OpenAIProviderError(
                    self._redact(f"request timed out after {self._timeout_s}s")
                )
            if attempt < self._max_retries:
                await asyncio.sleep(_BACKOFF_BASE_S * 2**attempt)
        attempts = self._max_retries + 1
        raise OpenAIProviderError(
            self._redact(f"request failed after {attempts} attempts: {last_error}")
        ) from last_error

    def _build_payload(
        self, messages: list[ChatMessage], tools: list[ToolSpec]
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [self._encode_message(message) for message in messages],
            "stream": False,
            "temperature": 0,
        }
        if tools:
            payload["tools"] = [self._encode_tool(tool) for tool in tools]
        return payload

    def _encode_message(self, message: ChatMessage) -> dict[str, Any]:
        if message.role == "tool":
            encoded: dict[str, Any] = {
                "role": "tool",
                "tool_call_id": message.tool_call_id,
                "content": message.content,
            }
            if message.name is not None:
                encoded["name"] = message.name
            return encoded
        if message.role == "assistant":
            encoded = {"role": "assistant", "content": message.content or None}
            if message.tool_calls:
                encoded["tool_calls"] = [
                    {
                        "id": call.call_id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments, ensure_ascii=False),
                        },
                    }
                    for call in message.tool_calls
                ]
            return encoded
        return {"role": message.role, "content": message.content}

    @staticmethod
    def _encode_tool(tool: ToolSpec) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }

    def _parse_response(self, data: dict[str, Any]) -> ModelResponse:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise OpenAIProviderError("response is missing choices")
        choice = choices[0]
        message = choice.get("message") if isinstance(choice, dict) else None
        if not isinstance(message, dict):
            raise OpenAIProviderError("response choice is missing a message")

        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise OpenAIProviderError("response content must be a string or null")

        raw_calls = message.get("tool_calls") or []
        if not isinstance(raw_calls, list):
            raise OpenAIProviderError("response tool_calls must be a list")
        tool_calls = [self._parse_tool_call(item) for item in raw_calls]

        try:
            return ModelResponse(
                content=content,
                tool_calls=tool_calls,
                finish_reason=choice.get("finish_reason") or "stop",
                usage=self._parse_usage(data.get("usage")),
            )
        except ValueError as exc:
            raise OpenAIProviderError(f"invalid model response: {exc}") from exc

    @staticmethod
    def _parse_tool_call(item: Any) -> ToolCall:
        if not isinstance(item, dict):
            raise OpenAIProviderError("tool call must be an object")
        call_id = item.get("id")
        if not isinstance(call_id, str) or not call_id:
            raise OpenAIProviderError("tool call is missing an id")
        function = item.get("function")
        if not isinstance(function, dict):
            raise OpenAIProviderError(f"tool call {call_id} is missing function")
        name = function.get("name")
        if not isinstance(name, str) or not name:
            raise OpenAIProviderError(f"tool call {call_id} is missing a name")

        arguments = function.get("arguments", "{}")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError as exc:
                raise OpenAIProviderError(
                    f"tool call {call_id} has invalid JSON arguments"
                ) from exc
        if not isinstance(arguments, dict):
            raise OpenAIProviderError(
                f"tool call {call_id} arguments must be an object"
            )
        return ToolCall(call_id=call_id, name=name, arguments=arguments)

    @classmethod
    def _parse_usage(cls, raw: Any) -> Usage:
        if not isinstance(raw, dict):
            return Usage()
        return Usage(
            input_tokens=cls._as_int(raw.get("prompt_tokens")),
            output_tokens=cls._as_int(raw.get("completion_tokens")),
            total_tokens=cls._as_int(raw.get("total_tokens")),
        )

    @staticmethod
    def _as_int(value: Any) -> int:
        if value is None:
            return 0
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise OpenAIProviderError("usage values must be integers") from exc

    def _redact(self, text: str) -> str:
        if not self._api_key:
            return text
        return text.replace(self._api_key, "***")

    def _urllib_transport(
        self, payload: dict[str, Any], timeout_s: float
    ) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self._base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            body = response.read().decode("utf-8")
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            raise OpenAIProviderError("response is not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise OpenAIProviderError("response is not a JSON object")
        return parsed
