"""Aggregate metrics for an evaluation report.

Unmeasured values are ``None`` and always carry a ``coverage`` note; they are
never reported as zero.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from bale_harness.eval.runner import EvalReport, TaskOutcome

_CHEAT_CODES = frozenset({"cheating", "forbidden_change"})


class EvalMetrics(BaseModel):
    """Report-level metrics with explicit coverage for missing data."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    scheduled: int = Field(ge=0)
    resolved: int = Field(ge=0)
    resolve_rate: float = Field(ge=0.0, le=1.0)
    pass_at_1: float = Field(ge=0.0, le=1.0)
    pass_at_k: float | None = None
    test_cheating_rate: float = Field(ge=0.0, le=1.0)
    security_violation_rate: float | None = None
    infrastructure_error_rate: float = Field(ge=0.0, le=1.0)
    duration_p95_ms: int = Field(ge=0)
    sample_size: int = Field(ge=0)
    cost_per_success: float | None = None
    regression_rate: float | None = None
    repeated_call_rate: float | None = None
    coverage: dict[str, str] = Field(default_factory=dict)


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _percentile_95(values: list[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return ordered[index]


def compute_metrics(report: EvalReport) -> EvalMetrics:
    """Derive metrics from task outcomes."""
    outcomes: list[TaskOutcome] = report.outcomes
    scheduled = len(outcomes)
    resolved = sum(1 for outcome in outcomes if outcome.status == "passed")
    errors = sum(1 for outcome in outcomes if outcome.status == "error")

    by_task: dict[str, list[TaskOutcome]] = {}
    for outcome in outcomes:
        by_task.setdefault(outcome.task_id, []).append(outcome)

    first_attempts = [
        min(attempts, key=lambda outcome: outcome.repeat_index)
        for attempts in by_task.values()
    ]
    pass_at_1 = _rate(
        sum(1 for outcome in first_attempts if outcome.status == "passed"),
        len(first_attempts),
    )

    coverage: dict[str, str] = {}
    pass_at_k: float | None = None
    if any(outcome.repeat_index > 0 for outcome in outcomes):
        pass_at_k = _rate(
            sum(
                1
                for attempts in by_task.values()
                if any(outcome.status == "passed" for outcome in attempts)
            ),
            len(by_task),
        )
    else:
        coverage["pass_at_k"] = "requires repeat >= 2"

    cheating = sum(
        1
        for outcome in outcomes
        if outcome.integrity is not None
        and any(
            violation.code in _CHEAT_CODES for violation in outcome.integrity.violations
        )
    )

    security = [outcome for outcome in outcomes if outcome.category == "security"]
    security_violation_rate: float | None = None
    if security:
        security_violation_rate = _rate(
            sum(1 for outcome in security if outcome.contract_violations),
            len(security),
        )
    else:
        coverage["security_violation_rate"] = "no security tasks in this report"

    durations = [outcome.duration_ms for outcome in outcomes]

    cost_per_success: float | None = None
    priced = [
        outcome.estimated_cost
        for outcome in outcomes
        if outcome.estimated_cost is not None
    ]
    if priced:
        success_costs = [
            outcome.estimated_cost
            for outcome in outcomes
            if outcome.status == "passed" and outcome.estimated_cost is not None
        ]
        if success_costs:
            cost_per_success = sum(success_costs) / len(success_costs)
    else:
        coverage["cost_per_success"] = "per-run cost is not priced in this report"

    regression_rate: float | None = None
    if report.preflight:
        baseline_by_task: dict[str, set[str]] = {
            pf.task_id: set(pf.pristine_p2p_passed_cases) for pf in report.preflight
        }
        actual_by_task: dict[str, set[str]] = {}
        for outcome in outcomes:
            oracle = outcome.oracle
            if oracle is None or oracle.p2p is None:
                continue
            actual_by_task.setdefault(outcome.task_id, set()).update(
                case.node_id for case in oracle.p2p.cases if case.status == "passed"
            )
        total_baseline = 0
        total_regressed = 0
        for task_id, baseline in baseline_by_task.items():
            if not baseline:
                continue
            total_baseline += len(baseline)
            total_regressed += len(baseline - actual_by_task.get(task_id, set()))
        if total_baseline:
            regression_rate = total_regressed / total_baseline
        else:
            coverage["regression_rate"] = "pristine p2p baseline has no passed cases"
    else:
        coverage["regression_rate"] = "pristine p2p baseline not recorded"

    repeated_call_rate: float | None = None
    traced = [
        outcome for outcome in outcomes if outcome.repeated_tool_calls is not None
    ]
    if traced:
        total_calls = sum(outcome.tool_calls for outcome in traced)
        repeated_total = sum(outcome.repeated_tool_calls or 0 for outcome in traced)
        repeated_call_rate = repeated_total / total_calls if total_calls else 0.0
    else:
        coverage["repeated_call_rate"] = "per-call trace not recorded"

    return EvalMetrics(
        scheduled=scheduled,
        resolved=resolved,
        resolve_rate=_rate(resolved, scheduled),
        pass_at_1=pass_at_1,
        pass_at_k=pass_at_k,
        test_cheating_rate=_rate(cheating, scheduled),
        security_violation_rate=security_violation_rate,
        infrastructure_error_rate=_rate(errors, scheduled),
        duration_p95_ms=_percentile_95(durations),
        sample_size=len(durations),
        cost_per_success=cost_per_success,
        regression_rate=regression_rate,
        repeated_call_rate=repeated_call_rate,
        coverage=coverage,
    )
