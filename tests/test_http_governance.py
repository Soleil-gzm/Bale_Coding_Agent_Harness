import asyncio
import json
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import uvicorn
from fastapi.testclient import TestClient

from bale_harness.agent.messages import (
    ChatMessage,
    ModelResponse,
    ToolCall,
    ToolSpec,
)
from bale_harness.agent.session import SessionStore
from bale_harness.http import create_app
from bale_harness.providers import FakeProvider
from bale_harness.settings import Settings


class _BlockingProvider:
    """Hold a run open until the test releases it."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(
        self, messages: list[ChatMessage], tools: list[ToolSpec]
    ) -> ModelResponse:
        self.started.set()
        await self.release.wait()
        return ModelResponse(content="done")


class _WriteOnceProvider:
    """Request one write, then answer once the tool result comes back."""

    def __init__(self, path: str = "out.txt") -> None:
        self._path = path
        self.calls = 0

    async def complete(
        self, messages: list[ChatMessage], tools: list[ToolSpec]
    ) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                tool_calls=[
                    ToolCall(
                        call_id="c1",
                        name="write_file",
                        arguments={"path": self._path, "content": "hello\n"},
                    )
                ]
            )
        return ModelResponse(content="done")


def _app(provider, **overrides: object):
    settings = Settings(_env_file=None, provider="fake", **overrides)
    return create_app(settings=settings, provider=provider)


def _payload(workspace: Path, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "message": "do work",
        "workspace": str(workspace),
        "shadow_git": False,
    }
    values.update(overrides)
    return values


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def _pending_approval(
    client: httpx.AsyncClient, run_id: str, timeout: float = 5.0
) -> dict[str, object]:
    """Poll the approvals endpoint until the broker publishes one."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        response = await client.get(f"/v1/runs/{run_id}/approvals")
        assert response.status_code == 200, response.text
        if response.json():
            return response.json()[0]
        await asyncio.sleep(0.01)
    raise AssertionError("the broker never announced an approval")


def test_multiple_api_keys_carry_principals_and_scopes(tmp_path: Path) -> None:
    app = _app(
        FakeProvider([ModelResponse(content="done")]),
        http_api_keys={
            "key-a": {"principal": "alice", "scope": "write"},
            "key-b": {"principal": "bob", "scope": "read"},
        },
    )

    with TestClient(app) as client:
        alice = client.post("/v1/runs", json=_payload(tmp_path), headers=_auth("key-a"))
        bob_write = client.post(
            "/v1/runs", json=_payload(tmp_path), headers=_auth("key-b")
        )
        bob_read = client.post(
            "/v1/runs",
            json=_payload(tmp_path, approval_mode="auto_read"),
            headers=_auth("key-b"),
        )

    assert alice.status_code == 200, alice.text
    assert alice.headers["x-bale-principal"] == "alice"
    assert bob_write.status_code == 403
    assert bob_read.status_code == 200, bob_read.text
    assert bob_read.headers["x-bale-principal"] == "bob"


def test_rate_limit_returns_429_and_spares_health(tmp_path: Path) -> None:
    app = _app(
        FakeProvider(),
        http_api_key="secret",
        http_rate_limit_per_minute=2,
    )

    with TestClient(app) as client:
        first = client.post(
            "/v1/runs", json=_payload(tmp_path), headers=_auth("secret")
        )
        second = client.post(
            "/v1/runs", json=_payload(tmp_path), headers=_auth("secret")
        )
        limited = client.post(
            "/v1/runs", json=_payload(tmp_path), headers=_auth("secret")
        )
        health = client.get("/health")

    assert first.status_code == 200
    assert second.status_code == 200
    assert limited.status_code == 429
    assert limited.headers["retry-after"]
    assert health.status_code == 200


def test_runs_share_a_persisted_session(tmp_path: Path) -> None:
    database = tmp_path / "sessions.db"
    session_id = uuid4()
    app = _app(
        FakeProvider([ModelResponse(content="one"), ModelResponse(content="two")]),
        http_session_db=database,
    )

    with TestClient(app) as client:
        first = client.post(
            "/v1/runs", json=_payload(tmp_path, session_id=str(session_id))
        )
        second = client.post(
            "/v1/runs", json=_payload(tmp_path, session_id=str(session_id))
        )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.headers["x-session-id"] == str(session_id)
    assert second.json()["session_id"] == str(session_id)

    async def load() -> int:
        return len(await SessionStore(database).list_turns(session_id))

    assert asyncio.run(load()) == 2


def test_session_id_requires_a_session_database(tmp_path: Path) -> None:
    app = _app(FakeProvider())

    with TestClient(app) as client:
        response = client.post(
            "/v1/runs", json=_payload(tmp_path, session_id=str(uuid4()))
        )

    assert response.status_code == 400


def test_concurrent_runs_cannot_share_a_session(tmp_path: Path) -> None:
    provider = _BlockingProvider()
    app = _app(provider, http_session_db=tmp_path / "sessions.db")
    session_id = str(uuid4())
    payload = _payload(tmp_path, session_id=session_id, run_id="run-1")

    async def scenario() -> tuple[int, int]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            first = asyncio.create_task(client.post("/v1/runs", json=payload))
            await asyncio.wait_for(provider.started.wait(), timeout=5)
            busy = await client.post(
                "/v1/runs",
                json=_payload(tmp_path, session_id=session_id, run_id="run-2"),
            )
            provider.release.set()
            started = await asyncio.wait_for(first, timeout=5)
            return started.status_code, busy.status_code

    assert asyncio.run(scenario()) == (200, 409)


def test_http_approval_allows_a_write(tmp_path: Path) -> None:
    app = _app(_WriteOnceProvider(), http_approvals=True)
    payload = _payload(tmp_path, run_id="run-1")

    async def scenario() -> tuple[dict[str, object], httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            run = asyncio.create_task(client.post("/v1/runs", json=payload))
            pending = await _pending_approval(client, "run-1")
            decision = await client.post(
                f"/v1/runs/run-1/approvals/{pending['approval_id']}",
                json={"decision": "approve"},
            )
            result = await asyncio.wait_for(run, timeout=5)
            assert result.status_code == 200, result.text
            return pending, decision

    pending, decision = asyncio.run(scenario())

    assert pending["tool"] == "write_file"
    assert pending["arguments"] == {"path": "out.txt", "content": "hello\n"}
    assert decision.status_code == 200, decision.text
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "hello\n"


def test_http_approval_deny_blocks_the_write(tmp_path: Path) -> None:
    app = _app(_WriteOnceProvider(), http_approvals=True)
    payload = _payload(tmp_path, run_id="run-1")

    async def scenario() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            run = asyncio.create_task(client.post("/v1/runs", json=payload))
            pending = await _pending_approval(client, "run-1")
            decision = await client.post(
                f"/v1/runs/run-1/approvals/{pending['approval_id']}",
                json={"decision": "deny"},
            )
            assert decision.status_code == 200, decision.text
            return await asyncio.wait_for(run, timeout=5)

    result = asyncio.run(scenario())

    assert result.status_code == 200, result.text
    assert not (tmp_path / "out.txt").exists()


def test_approval_timeout_denies_the_write(tmp_path: Path) -> None:
    app = _app(
        _WriteOnceProvider(),
        http_approvals=True,
        http_approval_timeout_seconds=1,
    )

    with TestClient(app) as client:
        response = client.post("/v1/runs", json=_payload(tmp_path))

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed"
    assert not (tmp_path / "out.txt").exists()


def test_read_only_keys_cannot_approve(tmp_path: Path) -> None:
    app = _app(
        FakeProvider(),
        http_approvals=True,
        http_api_keys={
            "key-b": {"principal": "bob", "scope": "read"},
        },
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/runs/run-1/approvals/missing",
            json={"decision": "approve"},
            headers=_auth("key-b"),
        )

    assert response.status_code == 403


def test_stream_announces_pending_approvals(tmp_path: Path) -> None:
    app = _app(_WriteOnceProvider(), http_approvals=True)
    payload = _payload(tmp_path, run_id="run-1")

    async def scenario() -> str:
        # SSE only streams incrementally over a real socket; the ASGI
        # transport buffers the whole body until the run finishes.
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
        )
        serving = asyncio.create_task(server.serve())
        try:
            while not server.started:
                await asyncio.sleep(0.01)
            port = server.servers[0].sockets[0].getsockname()[1]
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                async with client.stream(
                    "POST", "/v1/runs/stream", json=payload
                ) as response:
                    assert response.status_code == 200
                    chunks: list[str] = []
                    decided = False
                    async for chunk in response.aiter_text():
                        chunks.append(chunk)
                        if not decided and "approval_requested" in "".join(chunks):
                            decided = True
                            pending = await _pending_approval(client, "run-1")
                            await client.post(
                                f"/v1/runs/run-1/approvals/{pending['approval_id']}",
                                json={"decision": "approve"},
                            )
                    return "".join(chunks)
        finally:
            server.should_exit = True
            await serving

    body = asyncio.run(scenario())

    assert "approval_requested" in body
    assert '"status":"completed"' in body


def test_audit_log_records_principal_and_status(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    app = _app(
        FakeProvider([ModelResponse(content="done")]),
        http_api_key="secret",
        http_audit_log=audit_path,
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/runs", json=_payload(tmp_path), headers=_auth("secret")
        )

    assert response.status_code == 200
    events = [
        json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [event["event"] for event in events] == ["run_started", "run_finished"]
    assert events[0]["principal"] == "default"
    assert events[1]["status"] == "completed"
    assert events[1]["run_id"] == response.headers["x-run-id"]
    assert UUID(events[1]["session_id"])


def test_run_status_reports_running_then_completed(tmp_path: Path) -> None:
    provider = _BlockingProvider()
    app = _app(provider)
    payload = _payload(tmp_path, run_id="run-1")

    async def scenario() -> tuple[httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            run = asyncio.create_task(client.post("/v1/runs", json=payload))
            await asyncio.wait_for(provider.started.wait(), timeout=5)
            running = await client.get("/v1/runs/run-1")
            provider.release.set()
            completed_run = await asyncio.wait_for(run, timeout=5)
            assert completed_run.status_code == 200, completed_run.text
            completed = await client.get("/v1/runs/run-1")
            return running, completed

    running, completed = asyncio.run(scenario())

    assert running.status_code == 200, running.text
    assert running.json()["status"] == "running"
    assert running.json()["result"] is None
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "completed"
    assert completed.json()["result"]["answer"] == "done"


def test_run_status_enforces_principal_and_allows_read_scope(
    tmp_path: Path,
) -> None:
    app = _app(
        FakeProvider([ModelResponse(content="done")]),
        http_api_keys={
            "write-key": {"principal": "alice", "scope": "write"},
            "read-key": {"principal": "alice", "scope": "read"},
            "bob-key": {"principal": "bob", "scope": "read"},
        },
    )

    with TestClient(app) as client:
        run = client.post(
            "/v1/runs",
            json=_payload(tmp_path, run_id="run-1"),
            headers=_auth("write-key"),
        )
        assert run.status_code == 200, run.text
        same_principal = client.get("/v1/runs/run-1", headers=_auth("read-key"))
        other_principal = client.get("/v1/runs/run-1", headers=_auth("bob-key"))

    assert same_principal.status_code == 200, same_principal.text
    assert same_principal.json()["result"]["answer"] == "done"
    assert other_principal.status_code == 403
