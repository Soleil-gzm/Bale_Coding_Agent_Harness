"""Tests for frozen evaluation baselines (E4)."""

from pathlib import Path

import pytest

from bale_harness.eval import (
    BaselineError,
    DatasetFingerprint,
    EvalManifest,
    EvalReport,
    GitFingerprint,
    ModelFingerprint,
    PromptFingerprint,
    SandboxFingerprint,
    TaskOutcome,
    ToolFingerprint,
    freeze_baseline,
)


def _report(
    *, hermetic: bool = True, dirty: bool | None = False, passed: int = 1
) -> EvalReport:
    manifest = EvalManifest(
        run_id="run",
        created_at="2026-01-01T00:00:00+00:00",
        task_count=1,
        git=GitFingerprint(commit="abc123", dirty=dirty),
        model=ModelFingerprint(provider="fake", model="fake", base_url=""),
        prompt=PromptFingerprint(system_prompt_sha256="p", messages_sha256="m"),
        tools=ToolFingerprint(schema_sha256="t", names=["read_file"]),
        sandbox=SandboxFingerprint(
            backend="docker",
            image="img",
            network=False,
            memory="1g",
            cpus=1.0,
            pids_limit=256,
            user="1000:1000",
            image_digest="sha256:deadbeef",
        ),
        dataset=DatasetFingerprint(
            suite="golden", suite_version="1", content_sha256="d", file_count=1
        ),
        hermetic=hermetic,
    )
    outcome = TaskOutcome(
        task_id="t1",
        category="edit",
        repeat_index=0,
        status="passed",  # type: ignore[arg-type]
        duration_ms=1,
    )
    return EvalReport(
        started_at="t",
        duration_ms=1,
        total=1,
        passed=passed,
        failed=0,
        errors=0,
        outcomes=[outcome],
        manifest=manifest,
    )


def test_freeze_writes_report_and_roundtrips(tmp_path: Path) -> None:
    written = freeze_baseline(_report(), "golden-v1", baseline_dir=tmp_path)

    assert written == tmp_path / "golden-v1.json"
    loaded = EvalReport.model_validate_json(written.read_text(encoding="utf-8"))
    assert loaded.manifest is not None
    assert loaded.manifest.hermetic is True
    assert loaded.passed == 1


def test_freeze_refuses_overwrite(tmp_path: Path) -> None:
    freeze_baseline(_report(), "golden-v1", baseline_dir=tmp_path)

    with pytest.raises(BaselineError, match="already exists"):
        freeze_baseline(_report(), "golden-v1", baseline_dir=tmp_path)


def test_freeze_supersede_archives_previous(tmp_path: Path) -> None:
    freeze_baseline(_report(passed=1), "golden-v1", baseline_dir=tmp_path)
    freeze_baseline(
        _report(passed=2),
        "golden-v1",
        baseline_dir=tmp_path,
        supersede=True,
        supersede_reason="oracle bug fixed",
    )

    current = EvalReport.model_validate_json(
        (tmp_path / "golden-v1.json").read_text(encoding="utf-8")
    )
    assert current.passed == 2
    archive = tmp_path / "golden-v1.superseded.json"
    assert archive.is_file()
    old = EvalReport.model_validate_json(archive.read_text(encoding="utf-8"))
    assert old.passed == 1
    assert old.manifest is not None
    assert old.manifest.feature_flags["baseline_superseded"] is True
    assert (
        old.manifest.feature_flags["baseline_superseded_reason"] == "oracle bug fixed"
    )


def test_freeze_supersede_requires_reason(tmp_path: Path) -> None:
    freeze_baseline(_report(), "golden-v1", baseline_dir=tmp_path)

    with pytest.raises(BaselineError, match="supersede_reason"):
        freeze_baseline(_report(), "golden-v1", baseline_dir=tmp_path, supersede=True)


def test_freeze_refuses_non_hermetic(tmp_path: Path) -> None:
    with pytest.raises(BaselineError, match="hermetic"):
        freeze_baseline(_report(hermetic=False), "golden-v1", baseline_dir=tmp_path)


def test_freeze_refuses_dirty_tree(tmp_path: Path) -> None:
    with pytest.raises(BaselineError, match="dirty"):
        freeze_baseline(_report(dirty=True), "golden-v1", baseline_dir=tmp_path)


def test_freeze_refuses_missing_manifest(tmp_path: Path) -> None:
    report = _report().model_copy(update={"manifest": None})

    with pytest.raises(BaselineError, match="manifest"):
        freeze_baseline(report, "golden-v1", baseline_dir=tmp_path)


def test_freeze_allows_non_hermetic_when_disabled(tmp_path: Path) -> None:
    written = freeze_baseline(
        _report(hermetic=False, dirty=True),
        "debug-v1",
        baseline_dir=tmp_path,
        require_hermetic=False,
    )

    assert written.is_file()


def test_validate_baseline_name_rejects_traversal(tmp_path: Path) -> None:
    with pytest.raises(BaselineError, match="invalid baseline name"):
        freeze_baseline(_report(), "../escape", baseline_dir=tmp_path)
