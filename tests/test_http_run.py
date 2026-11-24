from pathlib import Path

from fastapi.testclient import TestClient

from bale_harness.agent.messages import ModelResponse, ToolCall
from bale_harness.http import create_app
from bale_harness.providers import FakeProvider
from bale_harness.settings import Settings


def _write_call() -> ModelResponse:
    return ModelResponse(
        tool_calls=[
            ToolCall(
                call_id="c1",
                name="write_file",
                arguments={"path": "a.txt", "content": "hello\n"},
            )
        ]
    )


def _client(provider: FakeProvider) -> TestClient:
    settings = Settings(_env_file=None, provider="fake")
    return TestClient(create_app(settings=settings, provider=provider))


def _payload(workspace: Path, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "message": "do work",
        "workspace": str(workspace),
        "shadow_git": False,
    }
    values.update(overrides)
    return values


def test_run_returns_completed_result(tmp_path: Path) -> None:
    provider = FakeProvider([ModelResponse(content="done")])

    with _client(provider) as client:
        response = client.post("/v1/runs", json=_payload(tmp_path))

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "completed"
    assert payload["answer"] == "done"
    assert payload["iterations"] == 1


def test_run_denies_writes_by_default(tmp_path: Path) -> None:
    provider = FakeProvider([_write_call(), ModelResponse(content="blocked")])

    with _client(provider) as client:
        response = client.post("/v1/runs", json=_payload(tmp_path))

    assert response.status_code == 200, response.text
    assert response.json()["answer"] == "blocked"
    assert not (tmp_path / "a.txt").exists()


def test_run_writes_when_explicitly_approved(tmp_path: Path) -> None:
    provider = FakeProvider([_write_call(), ModelResponse(content="wrote it")])

    with _client(provider) as client:
        response = client.post("/v1/runs", json=_payload(tmp_path, approve_writes=True))

    assert response.status_code == 200, response.text
    assert response.json()["answer"] == "wrote it"
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "hello\n"


def test_run_rejects_missing_workspace(tmp_path: Path) -> None:
    provider = FakeProvider()

    with _client(provider) as client:
        response = client.post(
            "/v1/runs",
            json=_payload(tmp_path / "missing"),
        )

    assert response.status_code == 422


def test_run_rejects_blank_message_and_unknown_fields(tmp_path: Path) -> None:
    provider = FakeProvider()

    with _client(provider) as client:
        blank = client.post("/v1/runs", json=_payload(tmp_path, message="   "))
        unknown = client.post("/v1/runs", json=_payload(tmp_path, extra=True))

    assert blank.status_code == 422
    assert unknown.status_code == 422


def test_stream_emits_trace_events_and_final_result(tmp_path: Path) -> None:
    provider = FakeProvider([ModelResponse(content="done")])

    with _client(provider) as client:
        with client.stream(
            "POST", "/v1/runs/stream", json=_payload(tmp_path)
        ) as response:
            body = "".join(response.iter_text())

    assert response.status_code == 200, body
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: trace" in body
    assert '"event":"turn_started"' in body
    assert '"event":"model_call"' in body
    assert "event: result" in body
    assert '"status":"completed"' in body
    assert body.index("turn_started") < body.index("event: result")


def test_stream_reports_status_denied_tool_calls(tmp_path: Path) -> None:
    provider = FakeProvider([_write_call(), ModelResponse(content="blocked")])

    with _client(provider) as client:
        with client.stream(
            "POST", "/v1/runs/stream", json=_payload(tmp_path)
        ) as response:
            body = "".join(response.iter_text())

    assert response.status_code == 200, body
    assert '"event":"tool_call"' in body
    assert '"status":"denied"' in body
    assert not (tmp_path / "a.txt").exists()
