"""Paired baseline comparison with explicit compatibility gates.

Two reports are only comparable when every effect-affecting fingerprint
matches, except for the one dimension named by ``treatment``. A dataset change
can never support an effect claim, and any infrastructure error invalidates the
comparison. Results are paired by ``(task_id, repeat_index)``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from bale_harness.eval.runner import EvalReport

Treatment = Literal["harness", "model", "prompt", "tooling", "sandbox", "dataset"]

TREATMENTS: frozenset[str] = frozenset(
    {"harness", "model", "prompt", "tooling", "sandbox", "dataset"}
)

# Fingerprint fields that are allowed to differ for each treatment.
_TREATMENT_FIELDS: dict[str, frozenset[str]] = {
    "harness": frozenset({"tools", "sandbox"}),
    "model": frozenset({"model"}),
    "prompt": frozenset({"prompt"}),
    "tooling": frozenset({"tools"}),
    "sandbox": frozenset({"sandbox"}),
    "dataset": frozenset(),
}

_INCONCLUSIVE_MARGIN = 2


class ComparisonVerdict(StrEnum):
    """Outcome of a paired comparison."""

    IMPROVED = "improved"
    REGRESSED = "regressed"
    UNCHANGED = "unchanged"
    INCONCLUSIVE = "inconclusive"
    INCOMPARABLE = "incomparable"


class TaskPair(BaseModel):
    """One paired task attempt."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    repeat_index: int = Field(ge=0)
    baseline: str
    candidate: str
    change: Literal["resolved", "regressed", "same"]


class CategoryDelta(BaseModel):
    """Passed-count difference for one category."""

    model_config = ConfigDict(extra="forbid")

    category: str
    baseline_passed: int = Field(ge=0)
    candidate_passed: int = Field(ge=0)
    delta: int


class Comparison(BaseModel):
    """Everything a report needs to describe a baseline comparison."""

    model_config = ConfigDict(extra="forbid")

    verdict: ComparisonVerdict
    treatment: str | None = None
    incompatible_reasons: list[str] = Field(default_factory=list)
    new_passes: list[str] = Field(default_factory=list)
    new_failures: list[str] = Field(default_factory=list)
    pairs: list[TaskPair] = Field(default_factory=list)
    category_deltas: list[CategoryDelta] = Field(default_factory=list)
    metrics_delta: dict[str, float | None] = Field(default_factory=dict)
    sample_size: int = Field(default=0, ge=0)


def _pair_key(task_id: str, repeat_index: int) -> str:
    return f"{task_id}#{repeat_index}"


def _compatibility_reasons(
    baseline: EvalReport, candidate: EvalReport, treatment: str | None
) -> list[str]:
    reasons: list[str] = []
    if treatment is not None and treatment not in TREATMENTS:
        reasons.append(f"unknown_treatment:{treatment}")
    if baseline.manifest is None or candidate.manifest is None:
        reasons.append("manifest")
        return reasons

    baseline_fields = baseline.manifest.comparability_fields()
    candidate_fields = candidate.manifest.comparability_fields()
    allowed = _TREATMENT_FIELDS.get(treatment or "", frozenset())
    for name, baseline_value in baseline_fields.items():
        candidate_value = candidate_fields.get(name)
        if baseline_value == candidate_value:
            continue
        if name not in allowed:
            reasons.append(name)
    if treatment == "dataset":
        reasons.append("dataset:treatment cannot support effect claims")

    baseline_keys = {
        (outcome.task_id, outcome.repeat_index) for outcome in baseline.outcomes
    }
    candidate_keys = {
        (outcome.task_id, outcome.repeat_index) for outcome in candidate.outcomes
    }
    if baseline_keys != candidate_keys:
        reasons.append("missing_pair")

    for report, label in ((baseline, "baseline"), (candidate, "candidate")):
        if report.metrics is not None and report.metrics.infrastructure_error_rate > 0:
            reasons.append(f"infrastructure_error:{label}")
    return reasons


def _category_deltas(
    baseline: EvalReport, candidate: EvalReport
) -> list[CategoryDelta]:
    def passed_by_category(report: EvalReport) -> dict[str, int]:
        counts: dict[str, int] = {}
        for outcome in report.outcomes:
            category = outcome.category or "uncategorized"
            counts.setdefault(category, 0)
            if outcome.status == "passed":
                counts[category] += 1
        return counts

    baseline_counts = passed_by_category(baseline)
    candidate_counts = passed_by_category(candidate)
    deltas: list[CategoryDelta] = []
    for category in sorted(set(baseline_counts) | set(candidate_counts)):
        before = baseline_counts.get(category, 0)
        after = candidate_counts.get(category, 0)
        deltas.append(
            CategoryDelta(
                category=category,
                baseline_passed=before,
                candidate_passed=after,
                delta=after - before,
            )
        )
    return deltas


def _metrics_delta(
    baseline: EvalReport, candidate: EvalReport
) -> dict[str, float | None]:
    before = baseline.metrics
    after = candidate.metrics
    if before is None or after is None:
        return {}
    delta: dict[str, float | None] = {}
    for name in ("resolve_rate", "pass_at_1", "pass_at_k"):
        baseline_value = getattr(before, name)
        candidate_value = getattr(after, name)
        delta[name] = (
            None
            if baseline_value is None or candidate_value is None
            else candidate_value - baseline_value
        )
    return delta


def compare_reports(
    baseline: EvalReport,
    candidate: EvalReport,
    treatment: str | None = None,
) -> Comparison:
    """Compare ``candidate`` against ``baseline`` after compatibility checks."""
    baseline_outcomes = {
        (outcome.task_id, outcome.repeat_index): outcome
        for outcome in baseline.outcomes
    }
    candidate_outcomes = {
        (outcome.task_id, outcome.repeat_index): outcome
        for outcome in candidate.outcomes
    }
    pairs: list[TaskPair] = []
    new_passes: list[str] = []
    new_failures: list[str] = []
    for task_id, repeat_index in sorted(
        set(baseline_outcomes) & set(candidate_outcomes)
    ):
        before = baseline_outcomes[(task_id, repeat_index)]
        after = candidate_outcomes[(task_id, repeat_index)]
        if before.status != "passed" and after.status == "passed":
            change: Literal["resolved", "regressed", "same"] = "resolved"
            new_passes.append(_pair_key(task_id, repeat_index))
        elif before.status == "passed" and after.status != "passed":
            change = "regressed"
            new_failures.append(_pair_key(task_id, repeat_index))
        else:
            change = "same"
        pairs.append(
            TaskPair(
                task_id=task_id,
                repeat_index=repeat_index,
                baseline=before.status,
                candidate=after.status,
                change=change,
            )
        )

    reasons = _compatibility_reasons(baseline, candidate, treatment)
    if reasons:
        verdict = ComparisonVerdict.INCOMPARABLE
    elif (
        new_passes
        and new_failures
        and abs(len(new_passes) - len(new_failures)) <= _INCONCLUSIVE_MARGIN
    ):
        verdict = ComparisonVerdict.INCONCLUSIVE
    elif new_failures:
        verdict = ComparisonVerdict.REGRESSED
    elif new_passes:
        verdict = ComparisonVerdict.IMPROVED
    else:
        verdict = ComparisonVerdict.UNCHANGED

    return Comparison(
        verdict=verdict,
        treatment=treatment,
        incompatible_reasons=reasons,
        new_passes=new_passes,
        new_failures=new_failures,
        pairs=pairs,
        category_deltas=_category_deltas(baseline, candidate),
        metrics_delta=_metrics_delta(baseline, candidate),
        sample_size=len(pairs),
    )
