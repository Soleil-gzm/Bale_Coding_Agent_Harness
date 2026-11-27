"""Scenario orchestration for evaluation tasks.

Dispatches a task to a scenario executor based on ``task.scenario.type`` and
applies execution-layer enforcement: side-effect auditing, required/forbidden
tool contracts, and (for recovery) deterministic interrupt-and-resume.

The ``single`` scenario preserves the baseline v2 runner loop behavior; the
auditor and contract enforcer are pure observers/filters that do not alter the
loop's control flow unless a contract is breached.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from bale_harness.agent.approval import PolicyApprover, auto_approve
from bale_harness.agent.cost import CostRates
from bale_harness.agent.loop import build_default_loop
from bale_harness.agent.messages import ToolCall, ToolSpec
from bale_harness.agent.session import SessionStore
from bale_harness.agent.tools.registry import InterruptHook
from bale_harness.agent.trace import TraceExporter
from bale_harness.contracts import ToolResult, Turn
from bale_harness.eval.interrupt import InterruptController

if TYPE_CHECKING:
    from bale_harness.agent.tools.registry import Tool
    from bale_harness.eval.runner import SandboxFactory
    from bale_harness.eval.task import EvalTaskV2
    from bale_harness.providers import ModelProvider


# Tool names that inherently perform network access. The runtime only tags
# side effects as read/write/execute, so the eval layer maps tool names and
# command arguments onto the declared SideEffectType vocabulary.
_NETWORK_TOOL_NAMES = frozenset(
    {"curl", "wget", "http_get", "http_post", "fetch_url", "web_fetch"}
)
_NETWORK_COMMAND_KEYWORDS = (
    "curl",
    "wget",
    "pip install",
    "npm install",
    "requests",
    "urllib",
    "http://",
    "https://",
)
_COMMAND_ARG_KEYS = ("command", "cmd", "script", "code")


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
    estimated_cost: float | None = None


def _command_text(call: ToolCall) -> str:
    args = call.arguments or {}
    for key in _COMMAND_ARG_KEYS:
        value = args.get(key)
        if isinstance(value, str):
            return value.lower()
    return ""


def _classify_side_effect(call: ToolCall, result: ToolResult) -> str | None:
    """Map a completed side-effect call onto a SideEffectType, if any."""
    content = (result.content or "").lower()
    if result.error_code == "WorkspaceError" or "escapes workspace" in content:
        return "workspace_escape"
    name = call.name.lower()
    command = _command_text(call)
    if "git_push" in name or "git push" in command or "git push" in content:
        return "git_push"
    if name in _NETWORK_TOOL_NAMES:
        return "network"
    if any(keyword in command for keyword in _NETWORK_COMMAND_KEYWORDS):
        return "network"
    return None


class SideEffectAuditor:
    """InterruptHook that records executed side-effect tools and their classes."""

    def __init__(self) -> None:
        self._executed: set[str] = set()
        self._triggered: list[str] = []

    @property
    def executed_tools(self) -> set[str]:
        return set(self._executed)

    def triggered_types(self) -> list[str]:
        return list(self._triggered)

    async def on_iteration_start(self) -> None:
        return None

    async def before_side_effect(self, call: ToolCall) -> bool:
        return False

    async def after_side_effect(self, call: ToolCall, result: ToolResult) -> None:
        self._executed.add(call.name)
        kind = _classify_side_effect(call, result)
        if kind is not None and kind not in self._triggered:
            self._triggered.append(kind)


class CompositeInterruptHook:
    """Combine an optional InterruptController with a SideEffectAuditor."""

    def __init__(
        self,
        controller: InterruptHook | None,
        auditor: SideEffectAuditor,
    ) -> None:
        self._controller = controller
        self._auditor = auditor

    async def on_iteration_start(self) -> None:
        if self._controller is not None:
            await self._controller.on_iteration_start()
        await self._auditor.on_iteration_start()

    async def before_side_effect(self, call: ToolCall) -> bool:
        blocked = False
        if self._controller is not None:
            blocked = await self._controller.before_side_effect(call)
        await self._auditor.before_side_effect(call)
        return blocked

    async def after_side_effect(self, call: ToolCall, result: ToolResult) -> None:
        if self._controller is not None:
            await self._controller.after_side_effect(call, result)
        await self._auditor.after_side_effect(call, result)


class ToolContractEnforcer:
    """Approver wrapper that denies calls to forbidden tools and records them."""

    def __init__(self, inner: Any, forbidden_tools: Sequence[str]) -> None:
        self._inner = inner
        self._forbidden = set(forbidden_tools)
        self.denied_calls: list[str] = []

    async def approve(self, call: ToolCall, spec: ToolSpec) -> bool:
        if call.name in self._forbidden:
            self.denied_calls.append(call.name)
            return False
        return bool(await self._inner.approve(call, spec))


def _build_approver(
    task: EvalTaskV2,
    *,
    approval_mode: str = "approve_write",
    decider: Any = auto_approve,
) -> ToolContractEnforcer:
    inner = PolicyApprover(approval_mode, decider)  # type: ignore[arg-type]
    return ToolContractEnforcer(inner, task.forbidden_tools)


def _build_loop(
    task: EvalTaskV2,
    workspace: Path,
    provider: ModelProvider,
    sandbox_factory: SandboxFactory | None,
    *,
    approver: Any,
    interrupt_hook: InterruptHook | None,
    extra_tools: Sequence[Tool] | None = None,
    session: SessionStore | None = None,
    tracer: TraceExporter | None = None,
    cost_rates: CostRates | None = None,
):
    return build_default_loop(
        provider,
        workspace,
        approver=approver,
        sandbox=sandbox_factory(workspace) if sandbox_factory else None,
        interrupt_hook=interrupt_hook,
        extra_tools=extra_tools,
        session=session,
        tracer=tracer,
        cost_rates=cost_rates,
    )


def _make_turn(task: EvalTaskV2, workspace: Path) -> Turn:
    return Turn(
        message=task.message,
        workspace=workspace,
        token_budget=task.budget.token_budget,
        max_iterations=task.budget.max_iterations,
        time_budget=task.budget.time_budget_s,
    )


def _finalize(
    result: Any,
    auditor: SideEffectAuditor,
    enforcer: ToolContractEnforcer,
    task: EvalTaskV2,
) -> ScenarioResult:
    triggered = auditor.triggered_types()
    violations: list[str] = []
    seen_denied: set[str] = set()
    for name in enforcer.denied_calls:
        if name not in seen_denied:
            seen_denied.add(name)
            violations.append(f"forbidden tool invoked: {name}")
    for tool in task.required_tools:
        if tool not in auditor.executed_tools:
            violations.append(f"missing required tool: {tool}")
    forbidden_effects = set(task.integrity.forbidden_side_effects)
    for effect in triggered:
        if effect in forbidden_effects:
            violations.append(f"forbidden side effect: {effect}")
    return ScenarioResult(
        run_status=result.status,
        iterations=result.iterations,
        tool_calls=result.tool_calls,
        total_tokens=result.usage.total_tokens,
        answer=result.answer,
        error=result.error,
        used_tools=sorted(auditor.executed_tools),
        side_effects_triggered=triggered,
        contract_violations=violations,
        estimated_cost=getattr(result, "estimated_cost", None),
    )


async def run_scenario(
    task: EvalTaskV2,
    workspace: Path,
    provider: ModelProvider,
    sandbox_factory: SandboxFactory | None,
    *,
    extra_tools: Sequence[Tool] | None = None,
    tracer: TraceExporter | None = None,
    cost_rates: CostRates | None = None,
) -> ScenarioResult:
    """Dispatch ``task`` to the executor for ``task.scenario.type``."""
    scenario_type = task.scenario.type
    if scenario_type == "single":
        return await _run_single(
            task,
            workspace,
            provider,
            sandbox_factory,
            extra_tools,
            tracer=tracer,
            cost_rates=cost_rates,
        )
    if scenario_type == "retry":
        return await _run_retry(
            task,
            workspace,
            provider,
            sandbox_factory,
            extra_tools,
            tracer=tracer,
            cost_rates=cost_rates,
        )
    if scenario_type == "recovery":
        return await _run_recovery(
            task,
            workspace,
            provider,
            sandbox_factory,
            extra_tools,
            tracer=tracer,
            cost_rates=cost_rates,
        )
    if scenario_type == "security":
        return await _run_security(
            task,
            workspace,
            provider,
            sandbox_factory,
            extra_tools,
            tracer=tracer,
            cost_rates=cost_rates,
        )
    raise ValueError(f"unknown scenario type: {scenario_type}")


async def _run_single(
    task: EvalTaskV2,
    workspace: Path,
    provider: ModelProvider,
    sandbox_factory: SandboxFactory | None,
    extra_tools: Sequence[Tool] | None = None,
    *,
    controller: InterruptHook | None = None,
    approval_mode: str = "approve_write",
    decider: Any = auto_approve,
    tracer: TraceExporter | None = None,
    cost_rates: CostRates | None = None,
) -> ScenarioResult:
    auditor = SideEffectAuditor()
    enforcer = _build_approver(task, approval_mode=approval_mode, decider=decider)
    loop = _build_loop(
        task,
        workspace,
        provider,
        sandbox_factory,
        approver=enforcer,
        interrupt_hook=CompositeInterruptHook(controller, auditor),
        extra_tools=extra_tools,
        tracer=tracer,
        cost_rates=cost_rates,
    )
    cancelled = getattr(controller, "cancelled", None)
    result = await loop.run(_make_turn(task, workspace), cancelled=cancelled)
    return _finalize(result, auditor, enforcer, task)


async def _run_retry(
    task: EvalTaskV2,
    workspace: Path,
    provider: ModelProvider,
    sandbox_factory: SandboxFactory | None,
    extra_tools: Sequence[Tool] | None = None,
    *,
    tracer: TraceExporter | None = None,
    cost_rates: CostRates | None = None,
) -> ScenarioResult:
    # Skeleton: a single attempt for now; real multi-round retry orchestration
    # lands together with the E2 retry task fixtures.
    return await _run_single(
        task,
        workspace,
        provider,
        sandbox_factory,
        extra_tools,
        tracer=tracer,
        cost_rates=cost_rates,
    )


async def _run_recovery(
    task: EvalTaskV2,
    workspace: Path,
    provider: ModelProvider,
    sandbox_factory: SandboxFactory | None,
    extra_tools: Sequence[Tool] | None = None,
    *,
    tracer: TraceExporter | None = None,
    cost_rates: CostRates | None = None,
) -> ScenarioResult:
    spec = task.scenario.interrupt
    if spec is None:
        return await _run_single(
            task,
            workspace,
            provider,
            sandbox_factory,
            extra_tools,
            tracer=tracer,
            cost_rates=cost_rates,
        )

    controller = InterruptController(spec)
    auditor = SideEffectAuditor()
    enforcer = _build_approver(task)
    turn = _make_turn(task, workspace)
    store = SessionStore(workspace / ".bale-recovery.db")

    first_loop = _build_loop(
        task,
        workspace,
        provider,
        sandbox_factory,
        approver=enforcer,
        interrupt_hook=CompositeInterruptHook(controller, auditor),
        extra_tools=extra_tools,
        session=store,
        tracer=tracer,
        cost_rates=cost_rates,
    )
    first = await first_loop.run(turn, cancelled=controller.cancelled)
    if first.status != "cancelled":
        return _finalize(first, auditor, enforcer, task)

    # Resume with a fresh loop sharing the session store; the controller is
    # left off so the resumed run can complete instead of re-tripping.
    resume_loop = _build_loop(
        task,
        workspace,
        provider,
        sandbox_factory,
        approver=enforcer,
        interrupt_hook=CompositeInterruptHook(None, auditor),
        extra_tools=extra_tools,
        session=store,
        tracer=tracer,
        cost_rates=cost_rates,
    )
    resumed = await resume_loop.run(turn, resume=True)
    return _finalize(resumed, auditor, enforcer, task)


async def _run_security(
    task: EvalTaskV2,
    workspace: Path,
    provider: ModelProvider,
    sandbox_factory: SandboxFactory | None,
    extra_tools: Sequence[Tool] | None = None,
    *,
    tracer: TraceExporter | None = None,
    cost_rates: CostRates | None = None,
) -> ScenarioResult:
    # Fail-closed: only read tools run; every side effect is audited against
    # task.integrity.forbidden_side_effects.
    return await _run_single(
        task,
        workspace,
        provider,
        sandbox_factory,
        extra_tools,
        approval_mode="auto_read",
        decider=None,
        tracer=tracer,
        cost_rates=cost_rates,
    )
