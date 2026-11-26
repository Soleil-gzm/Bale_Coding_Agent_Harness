"""Scenario orchestration for evaluation tasks.

Dispatches a task to a scenario executor based on ``task.scenario.type``. The
``single`` scenario is exactly equivalent to the baseline v2 runner loop;
``retry`` / ``recovery`` / ``security`` skeletons are wired here and hardened
with enforcement in the next commit.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from bale_harness.agent.approval import auto_approve
from bale_harness.agent.loop import build_default_loop
from bale_harness.contracts import Turn

if TYPE_CHECKING:
    from bale_harness.eval.runner import SandboxFactory
    from bale_harness.eval.task import EvalTaskV2
    from bale_harness.providers import ModelProvider


class ScenarioResult(BaseModel):
    """Outcome of running a task through a scenario executor."""

    model_config = ConfigDict(extra="forbid")

    run_status: str
    iterations: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    answer: str | None = None
    error: str | None = None
    used_tools: list[str] = Field(default_factory=list)
    side_effects_triggered: list[str] = Field(default_factory=list)
    contract_violations: list[str] = Field(default_factory=list)


async def run_scenario(
    task: EvalTaskV2,
    workspace: Path,
    provider: ModelProvider,
    sandbox_factory: SandboxFactory | None,
) -> ScenarioResult:
    """Dispatch ``task`` to the executor for ``task.scenario.type``."""
    scenario_type = task.scenario.type
    if scenario_type == "single":
        return await _run_single(task, workspace, provider, sandbox_factory)
    if scenario_type == "retry":
        return await _run_retry(task, workspace, provider, sandbox_factory)
    if scenario_type == "recovery":
        return await _run_recovery(task, workspace, provider, sandbox_factory)
    if scenario_type == "security":
        return await _run_security(task, workspace, provider, sandbox_factory)
    raise ValueError(f"unknown scenario type: {scenario_type}")


def _build_loop(
    task: EvalTaskV2,
    workspace: Path,
    provider: ModelProvider,
    sandbox_factory: SandboxFactory | None,
):
    return build_default_loop(
        provider,
        workspace,
        approver=auto_approve,
        sandbox=sandbox_factory(workspace) if sandbox_factory else None,
    )


def _make_turn(task: EvalTaskV2, workspace: Path) -> Turn:
    return Turn(
        message=task.message,
        workspace=workspace,
        token_budget=task.budget.token_budget,
        max_iterations=task.budget.max_iterations,
        time_budget=task.budget.time_budget_s,
    )


async def _run_single(
    task: EvalTaskV2,
    workspace: Path,
    provider: ModelProvider,
    sandbox_factory: SandboxFactory | None,
) -> ScenarioResult:
    loop = _build_loop(task, workspace, provider, sandbox_factory)
    result = await loop.run(_make_turn(task, workspace))
    return ScenarioResult(
        run_status=result.status,
        iterations=result.iterations,
        tool_calls=result.tool_calls,
        total_tokens=result.usage.total_tokens,
        answer=result.answer,
        error=result.error,
    )


async def _run_retry(
    task: EvalTaskV2,
    workspace: Path,
    provider: ModelProvider,
    sandbox_factory: SandboxFactory | None,
) -> ScenarioResult:
    # Skeleton: a single attempt for now; real multi-round retry orchestration
    # lands together with the E2 retry task fixtures.
    return await _run_single(task, workspace, provider, sandbox_factory)


async def _run_recovery(
    task: EvalTaskV2,
    workspace: Path,
    provider: ModelProvider,
    sandbox_factory: SandboxFactory | None,
) -> ScenarioResult:
    # Skeleton: delegates to single; interrupt injection and resume are wired
    # in the enforcement commit.
    return await _run_single(task, workspace, provider, sandbox_factory)


async def _run_security(
    task: EvalTaskV2,
    workspace: Path,
    provider: ModelProvider,
    sandbox_factory: SandboxFactory | None,
) -> ScenarioResult:
    # Skeleton: delegates to single; fail-closed approval and side-effect
    # auditing are wired in the enforcement commit.
    return await _run_single(task, workspace, provider, sandbox_factory)
