"""Metric computation tests, including null/coverage semantics (E3)."""

import pytest

from bale_harness.eval import EvalMetrics, EvalReport, TaskOutcome, compute_metrics
from bale_harness.eval.runner import IntegrityOutcome, ViolationOutcome


def _outcome(
    task_id: str,
    status: str,
    *,
    repeat: int = 0,
    duration: int = 100,
    category: str | None = None,
    cheating: bool = False,
    contract: list[str] | None = None,
) -> TaskOutcome:
    integrity = None
    if cheating:
        integrity = IntegrityOutcome(
            ok=False,
            violations=[
                ViolationOutcome(code="cheating", path="tests/a.py", detail="skip")
            ],
        )
    return TaskOutcome(
        task_id=task_id,
        category=category,
        repeat_index=repeat,
        status=status,  # type: ignore[arg-type]
        duration_ms=duration,
        integrity=integrity,
        contract_violations=contract or [],
    )


def _report(outcomes: list[TaskOutcome]) -> EvalReport:
    return EvalReport(
        started_at="2026-01-01T00:00:00+00:00",
        duration_ms=1,
        total=len(outcomes),
        passed=sum(1 for item in outcomes if item.status == "passed"),
        failed=sum(1 for item in outcomes if item.status == "failed"),
        errors=sum(1 for item in outcomes if item.status == "error"),
        outcomes=outcomes,
    )


def test_repeat_metrics_pass_at_1_and_k() -> None:
    report = _report(
        [
            _outcome("a", "failed", repeat=0),
            _outcome("a", "passed", repeat=1),
            _outcome("b", "passed", repeat=0),
            _outcome("b", "passed", repeat=1),
        ]
    )

    metrics = compute_metrics(report)

    assert isinstance(metrics, EvalMetrics)
    assert metrics.scheduled == 4
    assert metrics.resolved == 3
    assert metrics.resolve_rate == 0.75
    assert metrics.pass_at_1 == 0.5
    assert metrics.pass_at_k == 1.0
    assert metrics.sample_size == 4
    assert "pass_at_k" not in metrics.coverage


def test_single_run_reports_null_pass_at_k() -> None:
    metrics = compute_metrics(_report([_outcome("a", "passed")]))

    assert metrics.pass_at_k is None
    assert metrics.coverage["pass_at_k"] == "requires repeat >= 2"


def test_unmeasured_metrics_are_null_with_coverage() -> None:
    metrics = compute_metrics(_report([_outcome("a", "passed", category="read")]))

    assert metrics.cost_per_success is None
    assert metrics.regression_rate is None
    assert metrics.repeated_call_rate is None
    assert metrics.security_violation_rate is None
    assert "cost_per_success" in metrics.coverage
    assert "regression_rate" in metrics.coverage
    assert "repeated_call_rate" in metrics.coverage
    assert "security_violation_rate" in metrics.coverage


def test_security_and_cheating_rates() -> None:
    report = _report(
        [
            _outcome("s1", "failed", category="security", contract=["network"]),
            _outcome("s2", "passed", category="security"),
            _outcome("e1", "failed", category="edit", cheating=True),
            _outcome("r1", "passed", category="read"),
        ]
    )

    metrics = compute_metrics(report)

    assert metrics.security_violation_rate == 0.5
    assert metrics.test_cheating_rate == 0.25
    assert metrics.infrastructure_error_rate == 0.0


def test_duration_p95_and_error_rate() -> None:
    report = _report(
        [
            _outcome("a", "passed", duration=value)
            for value in (5, 10, 15, 20, 25, 30, 35, 40, 45, 100)
        ]
        + [_outcome("b", "error", duration=7)]
    )

    metrics = compute_metrics(report)

    # 11 samples: ceil(0.95 * 11) - 1 = 10 -> the maximum.
    assert metrics.duration_p95_ms == 100
    assert metrics.infrastructure_error_rate == pytest.approx(1 / 11)


def test_empty_report_is_zero_not_null() -> None:
    metrics = compute_metrics(_report([]))

    assert metrics.scheduled == 0
    assert metrics.resolve_rate == 0.0
    assert metrics.pass_at_1 == 0.0
    assert metrics.duration_p95_ms == 0
