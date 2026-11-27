import asyncio
import json
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from typer.testing import CliRunner

from bale_harness.agent.loop import build_default_loop
from bale_harness.agent.messages import ModelResponse, ToolCall, ToolSpec
from bale_harness.agent.session import SCHEMA, SessionStore
from bale_harness.cli import app
from bale_harness.contracts import ToolResult, Turn
from bale_harness.eval.task import EvalTask
from bale_harness.http import create_app
from bale_harness.providers import FakeProvider
from bale_harness.settings import Settings
from bale_harness.turns import TurnRequest

runner = CliRunner()


class SlowProvider:
    """Provider that always answers after a fixed delay."""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.calls = 0

    async def complete(self, messages, tools):
        self.calls += 1
        await asyncio.sleep(self.delay)
        return ModelResponse(content="too late")


class SlowTool:
    """Read-only tool that outlives the turn budget while running."""

    spec = ToolSpec(
        name="slow",
        description="Sleep for several seconds",
        parameters={"type": "object", "properties": {}},
        side_effect="read",
    )

    def __init__(self, delay: float = 5.0) -> None:
        self.delay = delay

    def validate_arguments(self, arguments: dict) -> None:
        return None

    async def execute(self, arguments: dict) -> ToolResult:
        await asyncio.sleep(self.delay)
        return ToolResult(
            call_id="",
            name="slow",
            status="success",
            content="finished",
            latency_ms=int(self.delay * 1000),
        )


def _turn(tmp_path: Path, **overrides: object) -> Turn:
    values: dict[str, object] = {
        "message": "do work",
        "workspace": tmp_path,
        "token_budget": 10_000,
        "max_iterations": 2,
    }
    values.update(overrides)
    return Turn(**values)


def test_time_budget_allows_fast_turn(tmp_path: Path) -> None:
    provider = FakeProvider([ModelResponse(content="done")])
    loop = build_default_loop(provider, tmp_path)

    result = asyncio.run(loop.run(_turn(tmp_path, time_budget=5.0)))

    assert result.status == "completed"
    assert result.answer == "done"


def test_time_budget_stops_slow_model_call(tmp_path: Path) -> None:
    provider = SlowProvider(5.0)
    loop = build_default_loop(provider, tmp_path)

    started = time.perf_counter()
    result = asyncio.run(loop.run(_turn(tmp_path, time_budget=0.05)))
    elapsed = time.perf_counter() - started

    assert result.status == "failed"
    assert result.error == "time budget exceeded"
    assert elapsed < 1.0
    assert result.duration_ms is not None


def test_time_budget_bounds_the_tool_batch(tmp_path: Path) -> None:
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=[ToolCall(call_id="c1", name="slow", arguments={})]
            ),
            ModelResponse(content="done"),
        ]
    )
    loop = build_default_loop(provider, tmp_path, extra_tools=[SlowTool()])

    started = time.perf_counter()
    result = asyncio.run(loop.run(_turn(tmp_path, time_budget=0.05)))
    elapsed = time.perf_counter() - started

    assert result.status == "failed"
    assert result.error == "time budget exceeded"
    assert elapsed < 2.0


def test_finish_in_a_slow_batch_cannot_beat_the_budget(tmp_path: Path) -> None:
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(call_id="c1", name="slow", arguments={}),
                    ToolCall(
                        call_id="c2",
                        name="finish",
                        arguments={"message": "done"},
                    ),
                ]
            ),
            ModelResponse(content="done"),
        ]
    )
    loop = build_default_loop(provider, tmp_path, extra_tools=[SlowTool()])

    result = asyncio.run(loop.run(_turn(tmp_path, time_budget=0.05)))

    assert result.status == "failed"
    assert result.error == "time budget exceeded"


def test_time_budget_checked_between_iterations(tmp_path: Path) -> None:
    provider = FakeProvider([ModelResponse(content="done")])
    loop = build_default_loop(provider, tmp_path)

    result = asyncio.run(loop.run(_turn(tmp_path, time_budget=1e-6)))

    assert result.status == "failed"
    assert result.error == "time budget exceeded"


def test_time_budget_must_be_positive() -> None:
    for value in (0, -1.0):
        with pytest.raises(ValidationError):
            _turn(Path("."), time_budget=value)
        with pytest.raises(ValidationError):
            TurnRequest(
                message="do work",
                workspace=Path("."),
                token_budget=1000,
                time_budget=value,
            )


def test_time_budget_persists_across_session_resume(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    provider = FakeProvider([ModelResponse(content="done")])
    loop = build_default_loop(provider, tmp_path, session=store)
    turn = _turn(tmp_path, time_budget=2.5)

    result = asyncio.run(loop.run(turn))
    assert result.status == "completed"

    record = asyncio.run(store.load_turn(turn.turn_id))
    assert record is not None
    assert record.time_budget == 2.5
    assert record.to_turn().time_budget == 2.5


def test_session_migrates_legacy_turns_table(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    legacy_schema = SCHEMA.replace("    time_budget REAL,\n", "")
    assert "time_budget" not in legacy_schema

    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(legacy_schema)
    finally:
        connection.close()

    store = SessionStore(db_path)
    turn = _turn(tmp_path, time_budget=1.5)
    asyncio.run(store.init())
    asyncio.run(store.ensure_session(turn.session_id, tmp_path))
    asyncio.run(store.start_turn(turn))

    record = asyncio.run(store.load_turn(turn.turn_id))
    assert record is not None
    assert record.time_budget == 1.5


def test_cli_turn_accepts_time_budget(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "turn",
            "--message",
            "hello",
            "--workspace",
            str(tmp_path),
            "--token-budget",
            "1000",
            "--time-budget",
            "2.5",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["time_budget"] == 2.5


def test_eval_task_accepts_time_budget() -> None:
    task = EvalTask(id="task", fixture=".", message="do work", time_budget=5)
    assert task.time_budget == 5

    with pytest.raises(ValidationError):
        EvalTask(id="task", fixture=".", message="do work", time_budget=0)


def test_http_rejects_non_positive_time_budget(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, provider="fake")
    provider = FakeProvider([ModelResponse(content="done")])

    with TestClient(create_app(settings=settings, provider=provider)) as client:
        response = client.post(
            "/v1/runs",
            json={
                "message": "do work",
                "workspace": str(tmp_path),
                "shadow_git": False,
                "time_budget": 0,
            },
        )

    assert response.status_code == 422


def test_http_run_honors_time_budget(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, provider="fake")
    provider = FakeProvider([ModelResponse(content="done")])

    with TestClient(create_app(settings=settings, provider=provider)) as client:
        response = client.post(
            "/v1/runs",
            json={
                "message": "do work",
                "workspace": str(tmp_path),
                "shadow_git": False,
                "time_budget": 30,
            },
        )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed"
