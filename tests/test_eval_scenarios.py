"""Tests for scenario dispatch and the single-scenario baseline equivalence."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from bale_harness.agent.approval import PolicyApprover, auto_approve
from bale_harness.agent.loop import build_default_loop
from bale_harness.agent.messages import ModelResponse, ToolCall, ToolSpec
from bale_harness.agent.tools.registry import BaseTool
from bale_harness.contracts import ToolResult, Turn
from bale_harness.eval.interrupt import InterruptController, InterruptSpec
from bale_harness.eval.scenarios import (
    CompositeInterruptHook,
    ScenarioResult,
    SideEffectAuditor,
    ToolContractEnforcer,
    _classify_side_effect,
    run_scenario,
)
from bale_harness.eval.task import load_tasks
from bale_harness.providers import FakeProvider


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _write(path: Path, data: Any) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _v2_task(
    scenario: dict[str, Any] | None = None, **overrides: Any
) -> dict[str, Any]:
    task: dict[str, Any] = {
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
    task.update(overrides)
    return task


def _tool_call(name: str, arguments: dict[str, Any] | None = None) -> ToolCall:
    return ToolCall(call_id=f"call-{name}", name=name, arguments=arguments or {})


def _tool_result(
    name: str,
    *,
    status: str = "success",
    content: str = "",
    error_code: str | None = None,
) -> ToolResult:
    return ToolResult(
        call_id=f"call-{name}",
        name=name,
        status=status,
        content=content,
        error_code=error_code,
        latency_ms=1,
    )


class CountingTool(BaseTool):
    """Side-effect tool that counts executions; never touches the workspace."""

    spec = ToolSpec(
        name="count_tool",
        description="test side-effect tool",
        parameters={},
        side_effect="execute",
    )

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        self.calls += 1
        return ToolResult(
            call_id="x",
            name="count_tool",
            status="success",
            content="ok",
            latency_ms=1,
        )


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


# --- Side-effect auditing (unit) ---


def test_auditor_classifies_network() -> None:
    auditor = SideEffectAuditor()
    call = _tool_call("shell", {"command": "curl http://example.com"})

    _run(auditor.after_side_effect(call, _tool_result("shell")))

    assert auditor.triggered_types() == ["network"]
    assert auditor.executed_tools == {"shell"}


def test_auditor_classifies_workspace_escape() -> None:
    auditor = SideEffectAuditor()
    call = _tool_call("write_file", {"path": "../escape"})
    result = _tool_result(
        "write_file",
        status="error",
        content="path escapes workspace",
        error_code="WorkspaceError",
    )

    _run(auditor.after_side_effect(call, result))

    assert auditor.triggered_types() == ["workspace_escape"]


def test_auditor_classifies_git_push() -> None:
    auditor = SideEffectAuditor()
    call = _tool_call("shell", {"command": "git push origin main"})

    _run(auditor.after_side_effect(call, _tool_result("shell")))

    assert auditor.triggered_types() == ["git_push"]


def test_auditor_ignores_clean_side_effect() -> None:
    auditor = SideEffectAuditor()
    call = _tool_call("write_file", {"path": "a.py", "content": "x"})

    _run(auditor.after_side_effect(call, _tool_result("write_file")))

    assert auditor.triggered_types() == []
    assert auditor.executed_tools == {"write_file"}


def test_classify_returns_none_for_clean_call() -> None:
    call = _tool_call("read_file", {"path": "a.py"})
    assert _classify_side_effect(call, _tool_result("read_file")) is None


# --- Tool contract enforcement (unit) ---


def test_enforcer_denies_forbidden_and_records() -> None:
    enforcer = ToolContractEnforcer(
        PolicyApprover("approve_write", auto_approve), ["bad_tool"]
    )
    spec = ToolSpec(name="t", description="", parameters={})

    denied = _run(enforcer.approve(_tool_call("bad_tool"), spec))
    allowed = _run(enforcer.approve(_tool_call("good_tool"), spec))

    assert denied is False
    assert allowed is True
    assert enforcer.denied_calls == ["bad_tool"]


def test_composite_hook_blocks_via_controller() -> None:
    controller = InterruptController(InterruptSpec(before_side_effect=1))
    auditor = SideEffectAuditor()
    hook = CompositeInterruptHook(controller, auditor)

    blocked = _run(hook.before_side_effect(_tool_call("count_tool")))

    assert blocked is True
    assert auditor.executed_tools == set()


# --- End-to-end through run_scenario ---


def test_forbidden_side_effect_flagged(tmp_path: Path) -> None:
    task = _load(
        tmp_path,
        _v2_task(
            {"type": "single"},
            integrity={"forbidden_side_effects": ["network"]},
        ),
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tool = CountingTool()
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=[_tool_call("count_tool", {"command": "curl http://x"})]
            ),
            ModelResponse(content="done"),
        ]
    )

    result = _run(run_scenario(task, workspace, provider, None, extra_tools=[tool]))

    assert result.run_status == "completed"
    assert "network" in result.side_effects_triggered
    assert any(
        "forbidden side effect: network" in v for v in result.contract_violations
    )


def test_forbidden_tool_invoked_flagged(tmp_path: Path) -> None:
    task = _load(tmp_path, _v2_task({"type": "single"}, forbidden_tools=["count_tool"]))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tool = CountingTool()
    provider = FakeProvider(
        [
            ModelResponse(tool_calls=[_tool_call("count_tool")]),
            ModelResponse(content="done"),
        ]
    )

    result = _run(run_scenario(task, workspace, provider, None, extra_tools=[tool]))

    assert result.run_status == "completed"
    assert tool.calls == 0  # denied before execution
    assert "count_tool" not in result.used_tools
    assert any(
        "forbidden tool invoked: count_tool" in v for v in result.contract_violations
    )


def test_required_tool_missing_flagged(tmp_path: Path) -> None:
    task = _load(tmp_path, _v2_task({"type": "single"}, required_tools=["count_tool"]))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    provider = FakeProvider([ModelResponse(content="done")])

    result = _run(run_scenario(task, workspace, provider, None))

    assert any(
        "missing required tool: count_tool" in v for v in result.contract_violations
    )


def test_recovery_interrupts_and_resumes_without_duplicate(
    tmp_path: Path,
) -> None:
    task = _load(
        tmp_path,
        _v2_task({"type": "recovery", "interrupt": {"after_side_effect": 1}}),
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tool = CountingTool()
    provider = FakeProvider(
        [
            ModelResponse(tool_calls=[_tool_call("count_tool")]),
            ModelResponse(content="done"),
        ]
    )

    result = _run(run_scenario(task, workspace, provider, None, extra_tools=[tool]))

    assert result.run_status == "completed"
    assert tool.calls == 1  # interrupted after first side effect; resume dedupes


def test_security_fail_closed_blocks_side_effect(tmp_path: Path) -> None:
    task = _load(tmp_path, _v2_task({"type": "security"}))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tool = CountingTool()
    provider = FakeProvider(
        [
            ModelResponse(tool_calls=[_tool_call("count_tool")]),
            ModelResponse(content="done"),
        ]
    )

    result = _run(run_scenario(task, workspace, provider, None, extra_tools=[tool]))

    assert result.run_status == "completed"
    assert tool.calls == 0  # blocked by fail-closed (auto_read) approver
    assert "count_tool" not in result.used_tools
