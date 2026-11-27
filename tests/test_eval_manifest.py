"""Reproducibility tests for the run manifest fingerprints (E3)."""

import json
import shutil
from pathlib import Path

from bale_harness.eval import (
    EvalReport,
    build_manifest,
    hash_dataset,
    load_suite,
)
from bale_harness.settings import Settings

GOLDEN_ROOT = Path(__file__).resolve().parents[1] / "examples" / "eval" / "golden"


def _tasks():
    _manifest, tasks = load_suite(GOLDEN_ROOT / "suite.json")
    return tasks


def test_manifest_is_reproducible_for_same_inputs() -> None:
    tasks = _tasks()
    settings = Settings()

    first = build_manifest(tasks, GOLDEN_ROOT, settings, run_id="fixed-run")
    second = build_manifest(tasks, GOLDEN_ROOT, settings, run_id="fixed-run")

    assert first.model_dump(exclude={"created_at"}) == second.model_dump(
        exclude={"created_at"}
    )
    assert first.dataset.content_sha256 == second.dataset.content_sha256
    assert len(first.dataset.content_sha256) == 64
    assert first.dataset.file_count > 0


def test_message_change_changes_dataset_hash() -> None:
    tasks = _tasks()

    original = hash_dataset(tasks, GOLDEN_ROOT).content_sha256
    mutated = [
        task.model_copy(update={"message": task.message + " extra"})
        if task.id == "read-func-behavior"
        else task
        for task in tasks
    ]

    assert hash_dataset(mutated, GOLDEN_ROOT).content_sha256 != original


def test_fixture_change_changes_dataset_hash(tmp_path: Path) -> None:
    copy_root = tmp_path / "golden"
    shutil.copytree(GOLDEN_ROOT, copy_root, ignore=shutil.ignore_patterns(".git"))
    tasks = _tasks()

    before = hash_dataset(tasks, copy_root).content_sha256
    target = copy_root / "fixtures" / "edit-add-negative" / "calc.py"
    target.write_text(target.read_text(encoding="utf-8") + "\n# drift\n")
    after = hash_dataset(tasks, copy_root).content_sha256

    assert before != after


def test_manifest_records_suite_and_sandbox_identity() -> None:
    manifest = build_manifest(
        _tasks(),
        GOLDEN_ROOT,
        Settings(),
        sandbox_backend="docker",
        sandbox_image="python:3.12-slim",
        suite="golden",
        suite_version="1.0.0",
        run_id="fixed-run",
    )

    assert manifest.dataset.suite == "golden"
    assert manifest.dataset.suite_version == "1.0.0"
    assert manifest.sandbox.backend == "docker"
    assert manifest.sandbox.image == "python:3.12-slim"
    assert manifest.sandbox.image_digest is None
    assert manifest.report_schema_version == 1
    assert manifest.task_count == 20


def test_manifest_never_contains_api_keys() -> None:
    settings = Settings(
        provider="openai",
        openai_api_key="sk-super-secret-value",
        openai_model="gpt-4o-mini",
    )

    manifest = build_manifest(_tasks(), GOLDEN_ROOT, settings, run_id="fixed-run")
    payload = json.dumps(manifest.model_dump(mode="json"))

    assert "sk-super-secret-value" not in payload
    assert "api_key" not in payload
    assert manifest.model.model == "gpt-4o-mini"


def test_report_carries_schema_version_and_manifest() -> None:
    report = EvalReport(
        started_at="2026-01-01T00:00:00+00:00",
        duration_ms=0,
        total=0,
        passed=0,
        failed=0,
        errors=0,
    )
    manifest = build_manifest(_tasks(), GOLDEN_ROOT, Settings(), run_id="fixed-run")
    report.manifest = manifest

    data = report.model_dump(mode="json")

    assert data["report_schema_version"] == 1
    assert data["manifest"]["run_id"] == "fixed-run"
    assert (
        data["manifest"]["dataset"]["content_sha256"] == manifest.dataset.content_sha256
    )


def test_git_fingerprint_tolerates_missing_or_dirty_tree() -> None:
    manifest = build_manifest(_tasks(), GOLDEN_ROOT, Settings(), run_id="fixed-run")

    assert manifest.git.commit is None or len(manifest.git.commit) == 40
    assert manifest.git.dirty in {True, False, None}
    assert "python" in manifest.feature_flags
