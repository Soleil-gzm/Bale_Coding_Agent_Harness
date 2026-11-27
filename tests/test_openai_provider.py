import asyncio
import json
import time
import urllib.error
from collections import deque
from typing import Any

import pytest

from bale_harness.agent.messages import (
    ChatMessage,
    ToolCall,
    ToolSpec,
    Usage,
)
from bale_harness.providers_openai import (
    OpenAICompatibleProvider,
    OpenAIProviderError,
)

API_KEY = "sk-test-super-secret-value"
MODEL = "deepseek-chat"
BASE_URL = "https://api.deepseek.com/v1"


class RecordingTransport:
    """Transport stub that records requests and replays scripted results."""

    def __init__(self, results: list[Any]) -> None:
        self.requests: list[tuple[dict[str, Any], float]] = []
        self._results: deque[Any] = deque(results)

    def __call__(self, payload: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        self.requests.append((payload, timeout_s))
        result = self._results.popleft()
        if isinstance(result, Exception):
            raise result
        return result


def _provider(
    transport: RecordingTransport, *, max_retries: int = 0, timeout_s: float = 5.0
) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        api_key=API_KEY,
        model=MODEL,
        base_url=BASE_URL,
        timeout_s=timeout_s,
        max_retries=max_retries,
        transport=transport,
    )


def _chat_response(
    *,
    content: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str = "stop",
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    body: dict[str, Any] = {
        "choices": [{"message": message, "finish_reason": finish_reason}]
    }
    if usage is not None:
        body["usage"] = usage
    return body


def _http_error(code: int, message: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(BASE_URL, code, message, None, None)


def _user(content: str = "hello") -> ChatMessage:
    return ChatMessage(role="user", content=content)


def test_request_body_maps_messages_tools_and_options() -> None:
    transport = RecordingTransport([_chat_response(content="done")])
    provider = _provider(transport)
    arguments = {"path": "a.txt", "note": "读取"}
    messages = [
        ChatMessage(role="system", content="You are helpful."),
        ChatMessage(role="user", content="Read a.txt"),
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(call_id="call-1", name="read_file", arguments=arguments)
            ],
        ),
        ChatMessage(
            role="tool",
            content='{"ok": true}',
            tool_call_id="call-1",
            name="read_file",
        ),
    ]
    tools = [
        ToolSpec(
            name="read_file",
            description="Read a file",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        )
    ]

    asyncio.run(provider.complete(messages, tools))

    payload, timeout_s = transport.requests[0]
    assert timeout_s == 5.0
    assert payload == {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Read a.txt"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps(arguments, ensure_ascii=False),
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "content": '{"ok": true}',
                "name": "read_file",
            },
        ],
        "stream": False,
        "temperature": 0,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read a file",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                },
            }
        ],
    }
    assert API_KEY not in json.dumps(payload, ensure_ascii=False)


def test_empty_tools_are_omitted_from_the_request() -> None:
    transport = RecordingTransport([_chat_response(content="ok")])
    provider = _provider(transport)
    messages = [
        _user("hi"),
        ChatMessage(role="assistant", content="sure"),
    ]

    asyncio.run(provider.complete(messages, []))

    payload, _ = transport.requests[0]
    assert "tools" not in payload
    assert payload["messages"] == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "sure"},
    ]


def test_parses_content_response_with_usage() -> None:
    transport = RecordingTransport(
        [
            _chat_response(
                content="hello",
                usage={
                    "prompt_tokens": 12,
                    "completion_tokens": 4,
                    "total_tokens": 16,
                },
            )
        ]
    )
    provider = _provider(transport)

    result = asyncio.run(provider.complete([_user()], []))

    assert result.content == "hello"
    assert result.finish_reason == "stop"
    assert not result.has_tool_calls
    assert result.usage == Usage(input_tokens=12, output_tokens=4, total_tokens=16)


def test_parses_tool_call_response() -> None:
    transport = RecordingTransport(
        [
            _chat_response(
                content=None,
                finish_reason="tool_calls",
                tool_calls=[
                    {
                        "id": "call-9",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": '{"path": "b.txt", "text": "hi"}',
                        },
                    }
                ],
            )
        ]
    )
    provider = _provider(transport)

    result = asyncio.run(provider.complete([_user()], []))

    assert result.content is None
    assert result.finish_reason == "tool_calls"
    assert result.has_tool_calls
    call = result.tool_calls[0]
    assert call.call_id == "call-9"
    assert call.name == "write_file"
    assert call.arguments == {"path": "b.txt", "text": "hi"}


def test_usage_defaults_to_zero_when_missing() -> None:
    transport = RecordingTransport([_chat_response(content="done")])
    provider = _provider(transport)

    result = asyncio.run(provider.complete([_user()], []))

    assert result.usage == Usage()


def test_invalid_tool_arguments_raise_provider_error() -> None:
    transport = RecordingTransport(
        [
            _chat_response(
                tool_calls=[
                    {
                        "id": "call-bad",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{not json"},
                    }
                ]
            )
        ]
    )
    provider = _provider(transport)

    with pytest.raises(OpenAIProviderError, match="invalid JSON") as error:
        asyncio.run(provider.complete([_user()], []))

    assert API_KEY not in str(error.value)


def test_invalid_response_shapes_raise_provider_error() -> None:
    provider = _provider(RecordingTransport([{"choices": []}]))
    with pytest.raises(OpenAIProviderError, match="missing choices"):
        asyncio.run(provider.complete([_user()], []))


def test_read_timeout_is_retried_and_wrapped() -> None:
    transport = RecordingTransport(
        [TimeoutError("read timed out"), TimeoutError("read timed out")]
    )
    provider = _provider(transport, max_retries=1)

    with pytest.raises(OpenAIProviderError, match="timed out") as error:
        asyncio.run(provider.complete([_user()], []))

    assert len(transport.requests) == 2
    assert "TimeoutError" not in str(error.value)

    provider = _provider(RecordingTransport([{"choices": [{}]}]))
    with pytest.raises(OpenAIProviderError, match="missing a message"):
        asyncio.run(provider.complete([_user()], []))

    provider = _provider(RecordingTransport([_chat_response(content="   ")]))
    with pytest.raises(OpenAIProviderError, match="invalid model response"):
        asyncio.run(provider.complete([_user()], []))


def test_retries_on_429_then_succeeds() -> None:
    transport = RecordingTransport(
        [
            _http_error(429, "Too Many Requests"),
            _chat_response(content="recovered"),
        ]
    )
    provider = _provider(transport, max_retries=1)

    started = time.monotonic()
    result = asyncio.run(provider.complete([_user()], []))
    elapsed = time.monotonic() - started

    assert result.content == "recovered"
    assert len(transport.requests) == 2
    assert elapsed >= 0.18


def test_retries_exhausted_on_server_errors_without_leaking_key() -> None:
    transport = RecordingTransport(
        [
            _http_error(500, f"boom {API_KEY}"),
            _http_error(503, f"down {API_KEY}"),
        ]
    )
    provider = _provider(transport, max_retries=1)

    with pytest.raises(OpenAIProviderError) as error:
        asyncio.run(provider.complete([_user()], []))

    message = str(error.value)
    assert "503" in message
    assert "***" in message
    assert API_KEY not in message
    assert len(transport.requests) == 2


def test_client_error_is_not_retried_and_redacts_key() -> None:
    transport = RecordingTransport([_http_error(401, f"invalid key {API_KEY}")])
    provider = _provider(transport, max_retries=2)

    with pytest.raises(OpenAIProviderError) as error:
        asyncio.run(provider.complete([_user()], []))

    message = str(error.value)
    assert "401" in message
    assert "***" in message
    assert API_KEY not in message
    assert len(transport.requests) == 1


def test_url_errors_are_retried_then_reported() -> None:
    transport = RecordingTransport(
        [
            urllib.error.URLError("connection refused"),
            urllib.error.URLError("connection refused"),
        ]
    )
    provider = _provider(transport, max_retries=1)

    with pytest.raises(OpenAIProviderError, match="connection refused") as error:
        asyncio.run(provider.complete([_user()], []))

    assert API_KEY not in str(error.value)
    assert len(transport.requests) == 2
