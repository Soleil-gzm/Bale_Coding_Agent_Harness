"""Tests for scenario dispatch and the single-scenario baseline equivalence."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from bale_harness.agent.loop import build_default_loop
from bale_harness.agent.messages import ModelResponse
from bale_harness.contracts import Turn
from bale_harness.eval.scenarios import ScenarioResult, run_scenario
from bale_harness.eval.task import load_tasks
from bale_harness.providers import FakeProvider


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _write(path: Path, data: Any) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _v2_task(scenario: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": "t1",
        "description": "d",
        "category": "edit",
        "message": "do it",
        "fixture": "fixture",
        "budget": {"max_iterations": 3, "token_budget": 10000},
        "oracle": {"file_checks": [{"type": "file_exists", "path": "a.py"}]},
        "integrity": {},
        "scenario": scenario or {"type": "single"},
    }


def _load(tmp_path: Path, task: dict[str, Any]):
    fixture = tmp_path / "fixture"
    fixture.mkdir(exist_ok=True)
    path = _write(tmp_path / "tasks.json", {"schema_version": 2, "tasks": [task]})
    return load_tasks(path)[0]


@pytest.mark.parametrize("scenario_type", ["single", "retry", "recovery", "security"])
def test_scenario_types_dispatch(tmp_path: Path, scenario_type: str) -> None:
    task = _load(tmp_path, _v2_task({"type": scenario_type}))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    provider = FakeProvider([ModelResponse(content="done")])

    result = _run(run_scenario(task, workspace, provider, None))

    assert isinstance(result, ScenarioResult)
    assert result.run_status == "completed"


def test_single_matches_direct_loop(tmp_path: Path) -> None:
    task = _load(tmp_path, _v2_task())
    workspace = tmp_path / "ws"
    workspace.mkdir()

    via_scenario = _run(
        run_scenario(task, workspace, FakeProvider([ModelResponse(content="x")]), None)
    )

    loop = build_default_loop(FakeProvider([ModelResponse(content="x")]), workspace)
    turn = Turn(
        message=task.message,
        workspace=workspace,
        token_budget=task.budget.token_budget,
        max_iterations=task.budget.max_iterations,
        time_budget=task.budget.time_budget_s,
    )
    direct = _run(loop.run(turn))

    assert via_scenario.run_status == direct.status
    assert via_scenario.iterations == direct.iterations
    assert via_scenario.tool_calls == direct.tool_calls
    assert via_scenario.total_tokens == direct.usage.total_tokens


def test_scenario_spec_interrupt_parses(tmp_path: Path) -> None:
    task = _load(
        tmp_path,
        _v2_task({"type": "recovery", "interrupt": {"after_side_effect": 1}}),
    )

    assert task.scenario.type == "recovery"
    assert task.scenario.interrupt is not None
    assert task.scenario.interrupt.after_side_effect == 1
