"""Paired comparison and compatibility-gate tests (E3)."""

from bale_harness.eval import (
    ComparisonVerdict,
    DatasetFingerprint,
    EvalManifest,
    EvalReport,
    ModelFingerprint,
    PromptFingerprint,
    SandboxFingerprint,
    TaskOutcome,
    ToolFingerprint,
    compare_reports,
    compute_metrics,
)


def _manifest(
    *,
    dataset: str = "ds",
    model: str = "gpt",
    tools: str = "tools",
    sandbox: str = "local",
    schema_version: int = 1,
) -> EvalManifest:
    return EvalManifest(
        report_schema_version=schema_version,
        run_id="run",
        created_at="2026-01-01T00:00:00+00:00",
        task_count=1,
        model=ModelFingerprint(provider="fake", model=model, base_url=""),
        prompt=PromptFingerprint(system_prompt_sha256="p", messages_sha256="m"),
        tools=ToolFingerprint(schema_sha256=tools, names=["read_file"]),
        sandbox=SandboxFingerprint(
            backend="local",
            image=sandbox,
            network=False,
            memory="1g",
            cpus=1.0,
            pids_limit=256,
            user="1000:1000",
        ),
        dataset=DatasetFingerprint(
            suite="s", suite_version="1", content_sha256=dataset, file_count=1
        ),
    )


def _outcome(
    task_id: str, status: str, *, repeat: int = 0, category: str = "edit"
) -> TaskOutcome:
    return TaskOutcome(
        task_id=task_id,
        category=category,
        repeat_index=repeat,
        status=status,  # type: ignore[arg-type]
        duration_ms=1,
    )


def _report(
    outcomes: list[TaskOutcome], manifest: EvalManifest | None = None
) -> EvalReport:
    report = EvalReport(
        started_at="t",
        duration_ms=1,
        total=len(outcomes),
        passed=sum(1 for item in outcomes if item.status == "passed"),
        failed=sum(1 for item in outcomes if item.status == "failed"),
        errors=sum(1 for item in outcomes if item.status == "error"),
        outcomes=outcomes,
    )
    report.metrics = compute_metrics(report)
    report.manifest = manifest
    return report


def test_identical_runs_are_unchanged() -> None:
    report = _report([_outcome("a", "passed")], _manifest())

    comparison = compare_reports(report, report)

    assert comparison.verdict is ComparisonVerdict.UNCHANGED
    assert comparison.sample_size == 1
    assert comparison.incompatible_reasons == []


def test_dataset_change_is_incomparable() -> None:
    baseline = _report([_outcome("a", "passed")], _manifest(dataset="one"))
    candidate = _report([_outcome("a", "passed")], _manifest(dataset="two"))

    comparison = compare_reports(baseline, candidate)

    assert comparison.verdict is ComparisonVerdict.INCOMPARABLE
    assert "dataset" in comparison.incompatible_reasons


def test_undeclared_model_change_is_incomparable() -> None:
    baseline = _report([_outcome("a", "passed")], _manifest(model="small"))
    candidate = _report([_outcome("a", "passed")], _manifest(model="large"))

    blocked = compare_reports(baseline, candidate)
    allowed = compare_reports(baseline, candidate, treatment="model")

    assert blocked.verdict is ComparisonVerdict.INCOMPARABLE
    assert "model" in blocked.incompatible_reasons
    assert allowed.verdict is ComparisonVerdict.UNCHANGED


def test_dataset_treatment_never_supports_effect_claims() -> None:
    baseline = _report([_outcome("a", "passed")], _manifest(dataset="one"))
    candidate = _report([_outcome("a", "passed")], _manifest(dataset="two"))

    comparison = compare_reports(baseline, candidate, treatment="dataset")

    assert comparison.verdict is ComparisonVerdict.INCOMPARABLE
    assert any("dataset" in reason for reason in comparison.incompatible_reasons)


def test_new_pass_is_improved_and_lists_the_pair() -> None:
    baseline = _report([_outcome("a", "failed")], _manifest())
    candidate = _report([_outcome("a", "passed")], _manifest())

    comparison = compare_reports(baseline, candidate)

    assert comparison.verdict is ComparisonVerdict.IMPROVED
    assert comparison.new_passes == ["a#0"]
    assert comparison.new_failures == []
    delta = comparison.category_deltas[0]
    assert (delta.category, delta.delta) == ("edit", 1)


def test_new_failure_is_regressed() -> None:
    baseline = _report([_outcome("a", "passed")], _manifest())
    candidate = _report([_outcome("a", "failed")], _manifest())

    comparison = compare_reports(baseline, candidate)

    assert comparison.verdict is ComparisonVerdict.REGRESSED
    assert comparison.new_failures == ["a#0"]


def test_balanced_changes_are_inconclusive() -> None:
    baseline = _report([_outcome("a", "failed"), _outcome("b", "passed")], _manifest())
    candidate = _report([_outcome("a", "passed"), _outcome("b", "failed")], _manifest())

    comparison = compare_reports(baseline, candidate)

    assert comparison.verdict is ComparisonVerdict.INCONCLUSIVE


def test_missing_pair_is_incomparable() -> None:
    baseline = _report([_outcome("a", "passed"), _outcome("b", "passed")], _manifest())
    candidate = _report([_outcome("a", "passed")], _manifest())

    comparison = compare_reports(baseline, candidate)

    assert comparison.verdict is ComparisonVerdict.INCOMPARABLE
    assert "missing_pair" in comparison.incompatible_reasons


def test_infrastructure_error_blocks_attribution() -> None:
    baseline = _report([_outcome("a", "passed")], _manifest())
    candidate = _report([_outcome("a", "error")], _manifest())

    comparison = compare_reports(baseline, candidate)

    assert comparison.verdict is ComparisonVerdict.INCOMPARABLE
    assert any(
        reason.startswith("infrastructure_error")
        for reason in comparison.incompatible_reasons
    )


def test_schema_version_mismatch_is_incomparable() -> None:
    baseline = _report([_outcome("a", "passed")], _manifest(schema_version=1))
    candidate = _report([_outcome("a", "passed")], _manifest(schema_version=2))

    comparison = compare_reports(baseline, candidate)

    assert comparison.verdict is ComparisonVerdict.INCOMPARABLE
    assert "report_schema_version" in comparison.incompatible_reasons


def test_missing_manifest_is_incomparable() -> None:
    baseline = _report([_outcome("a", "passed")], None)
    candidate = _report([_outcome("a", "passed")], _manifest())

    comparison = compare_reports(baseline, candidate)

    assert comparison.verdict is ComparisonVerdict.INCOMPARABLE
    assert "manifest" in comparison.incompatible_reasons
