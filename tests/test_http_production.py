import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from bale_harness.agent.messages import (
    ChatMessage,
    ModelResponse,
    ToolCall,
    ToolSpec,
)
from bale_harness.http import create_app
from bale_harness.providers import FakeProvider
from bale_harness.settings import Settings


class _EndlessProvider:
    """Issue read calls until the loop observes a cancellation request."""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def complete(
        self, messages: list[ChatMessage], tools: list[ToolSpec]
    ) -> ModelResponse:
        self.started.set()
        await asyncio.sleep(0.005)
        return ModelResponse(
            tool_calls=[
                ToolCall(
                    call_id="",
                    name="read_file",
                    arguments={"path": "missing.txt"},
                )
            ]
        )


def _app(
    provider: FakeProvider | _EndlessProvider,
    *,
    api_key: str | None = None,
):
    values: dict[str, object] = {"provider": "fake"}
    if api_key is not None:
        values["http_api_key"] = api_key
    settings = Settings(_env_file=None, **values)
    return create_app(settings=settings, provider=provider)


def _payload(workspace: Path, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "message": "do work",
        "workspace": str(workspace),
        "shadow_git": False,
    }
    values.update(overrides)
    return values


def test_health_is_public_even_with_api_key() -> None:
    app = _app(FakeProvider(), api_key="secret")

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_v1_routes_require_bearer_token_when_configured(tmp_path: Path) -> None:
    app = _app(FakeProvider([ModelResponse(content="done")]), api_key="secret")

    with TestClient(app) as client:
        missing = client.post("/v1/runs", json=_payload(tmp_path))
        wrong = client.post(
            "/v1/runs",
            json=_payload(tmp_path),
            headers={"Authorization": "Bearer nope"},
        )
        accepted = client.post(
            "/v1/runs",
            json=_payload(tmp_path),
            headers={"Authorization": "Bearer secret"},
        )

    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"
    assert wrong.status_code == 401
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["answer"] == "done"


def test_v1_routes_are_open_without_api_key(tmp_path: Path) -> None:
    app = _app(FakeProvider([ModelResponse(content="done")]))

    with TestClient(app) as client:
        response = client.post("/v1/runs", json=_payload(tmp_path))

    assert response.status_code == 200, response.text


def test_run_returns_generated_run_id_header(tmp_path: Path) -> None:
    app = _app(FakeProvider([ModelResponse(content="done")]))

    with TestClient(app) as client:
        response = client.post("/v1/runs", json=_payload(tmp_path))

    assert response.status_code == 200
    assert response.headers["x-run-id"]


def test_stream_reports_the_client_supplied_run_id(tmp_path: Path) -> None:
    app = _app(FakeProvider([ModelResponse(content="done")]))

    with TestClient(app) as client:
        with client.stream(
            "POST",
            "/v1/runs/stream",
            json=_payload(tmp_path, run_id="run-1"),
        ) as response:
            body = "".join(response.iter_text())

    assert response.status_code == 200, body
    assert response.headers["x-run-id"] == "run-1"
    assert 'event: run\ndata: {"run_id": "run-1"}' in body
    assert body.index("event: run") < body.index("event: result")


def test_cancel_unknown_run_returns_404() -> None:
    app = _app(FakeProvider())

    with TestClient(app) as client:
        response = client.post("/v1/runs/missing/cancel")

    assert response.status_code == 404


def test_stream_run_can_be_cancelled_by_run_id(tmp_path: Path) -> None:
    provider = _EndlessProvider()
    app = _app(provider)
    payload = _payload(tmp_path, run_id="run-1", max_iterations=200)

    async def scenario() -> str:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:

            async def consume() -> str:
                async with client.stream(
                    "POST", "/v1/runs/stream", json=payload
                ) as response:
                    assert response.status_code == 200
                    return "".join([part async for part in response.aiter_text()])

            stream_task = asyncio.create_task(consume())
            await asyncio.wait_for(provider.started.wait(), timeout=5)
            cancel = await client.post("/v1/runs/run-1/cancel")
            assert cancel.status_code == 200, cancel.text
            assert cancel.json() == {"run_id": "run-1", "cancelled": True}
            return await asyncio.wait_for(stream_task, timeout=5)

    body = asyncio.run(scenario())

    assert "event: run" in body
    assert '"status":"cancelled"' in body
    assert "event: result" in body


def test_cancel_after_completion_returns_404(tmp_path: Path) -> None:
    app = _app(FakeProvider([ModelResponse(content="done")]))

    with TestClient(app) as client:
        run = client.post("/v1/runs", json=_payload(tmp_path, run_id="run-1"))
        assert run.status_code == 200
        cancel = client.post("/v1/runs/run-1/cancel")

    assert cancel.status_code == 404


def test_invalid_run_id_is_rejected(tmp_path: Path) -> None:
    app = _app(FakeProvider())

    with TestClient(app) as client:
        response = client.post("/v1/runs", json=_payload(tmp_path, run_id="bad id"))

    assert response.status_code == 422


@pytest.mark.parametrize("run_id", [None, "run-1"])
def test_runs_record_usage_and_status(tmp_path: Path, run_id: str | None) -> None:
    app = _app(FakeProvider([ModelResponse(content="done")]))

    with TestClient(app) as client:
        response = client.post("/v1/runs", json=_payload(tmp_path, run_id=run_id))

    assert response.status_code == 200
    assert response.json()["status"] == "completed"
