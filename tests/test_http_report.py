from pathlib import Path

from fastapi.testclient import TestClient

from bale_harness.agent.messages import ModelResponse
from bale_harness.http import create_app
from bale_harness.providers import FakeProvider
from bale_harness.settings import HttpApiKey, Settings

AUTH = {"Authorization": "Bearer secret"}


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "provider": "fake",
        "http_session_db": tmp_path / "sessions.db",
        "http_api_key": "secret",
        "trace_path": str(tmp_path / "trace.jsonl"),
    }
    values.update(overrides)
    return Settings(**values)


def _run(
    client: TestClient, workspace: Path, message: str = "do work", run_id: str = "run-1"
):
    return client.post(
        "/v1/runs",
        json={
            "message": message,
            "workspace": str(workspace),
            "shadow_git": False,
            "run_id": run_id,
        },
        headers=AUTH,
    )


def test_report_renders_session_markdown(tmp_path: Path) -> None:
    provider = FakeProvider([ModelResponse(content="done")])
    with TestClient(
        create_app(settings=_settings(tmp_path), provider=provider)
    ) as client:
        run = _run(client, tmp_path)
        assert run.status_code == 200, run.text
        session_id = run.json()["session_id"]

        response = client.get("/v1/runs/run-1/report", headers=AUTH)

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/markdown")
    assert response.headers["x-run-id"] == "run-1"
    assert response.headers["x-session-id"] == session_id
    text = response.text
    assert "# Bale Run Report" in text
    assert session_id in text
    assert "done" in text


def test_report_requires_session_database(tmp_path: Path) -> None:
    provider = FakeProvider([ModelResponse(content="done")])
    settings = Settings(_env_file=None, provider="fake", http_api_key="secret")

    with TestClient(create_app(settings=settings, provider=provider)) as client:
        assert _run(client, tmp_path).status_code == 200
        response = client.get("/v1/runs/run-1/report", headers=AUTH)

    assert response.status_code == 400
    assert "BALE_HTTP_SESSION_DB" in response.json()["detail"]


def test_report_returns_404_for_unknown_run(tmp_path: Path) -> None:
    provider = FakeProvider([ModelResponse(content="done")])

    with TestClient(
        create_app(settings=_settings(tmp_path), provider=provider)
    ) as client:
        response = client.get("/v1/runs/missing/report", headers=AUTH)

    assert response.status_code == 404


def test_report_requires_authentication(tmp_path: Path) -> None:
    provider = FakeProvider([ModelResponse(content="done")])

    with TestClient(
        create_app(settings=_settings(tmp_path), provider=provider)
    ) as client:
        response = client.get("/v1/runs/run-1/report")

    assert response.status_code == 401


def test_report_rejects_other_principals(tmp_path: Path) -> None:
    keys = {
        "key-alice": HttpApiKey(principal="alice"),
        "key-bob": HttpApiKey(principal="bob"),
    }
    provider = FakeProvider([ModelResponse(content="done")])
    settings = _settings(tmp_path, http_api_key=None, http_api_keys=keys)

    with TestClient(create_app(settings=settings, provider=provider)) as client:
        run = client.post(
            "/v1/runs",
            json={
                "message": "do work",
                "workspace": str(tmp_path),
                "shadow_git": False,
                "run_id": "run-1",
            },
            headers={"Authorization": "Bearer key-alice"},
        )
        assert run.status_code == 200, run.text
        response = client.get(
            "/v1/runs/run-1/report",
            headers={"Authorization": "Bearer key-bob"},
        )

    assert response.status_code == 403


def test_read_scope_key_can_read_same_principal_report(tmp_path: Path) -> None:
    keys = {
        "write-key": HttpApiKey(principal="alice", scope="write"),
        "read-key": HttpApiKey(principal="alice", scope="read"),
    }
    provider = FakeProvider([ModelResponse(content="done")])
    settings = _settings(tmp_path, http_api_key=None, http_api_keys=keys)

    with TestClient(create_app(settings=settings, provider=provider)) as client:
        run = client.post(
            "/v1/runs",
            json={
                "message": "do work",
                "workspace": str(tmp_path),
                "shadow_git": False,
                "run_id": "run-1",
            },
            headers={"Authorization": "Bearer write-key"},
        )
        assert run.status_code == 200, run.text
        response = client.get(
            "/v1/runs/run-1/report",
            headers={"Authorization": "Bearer read-key"},
        )

    assert response.status_code == 200
    assert "# Bale Run Report" in response.text


def test_report_only_contains_the_requested_session(tmp_path: Path) -> None:
    provider = FakeProvider(
        [ModelResponse(content="first answer"), ModelResponse(content="second answer")]
    )

    with TestClient(
        create_app(settings=_settings(tmp_path), provider=provider)
    ) as client:
        assert _run(client, tmp_path, "first task", "run-1").status_code == 200
        assert _run(client, tmp_path, "second task", "run-2").status_code == 200

        first = client.get("/v1/runs/run-1/report", headers=AUTH)
        second = client.get("/v1/runs/run-2/report", headers=AUTH)

    assert first.status_code == 200
    assert second.status_code == 200
    assert "first task" in first.text
    assert "second task" not in first.text
    assert "second task" in second.text
    assert "first task" not in second.text
