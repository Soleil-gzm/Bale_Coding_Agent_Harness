"""CLI tests for the ``eval --suite`` entry point and its gates."""

import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from bale_harness.cli import app

runner = CliRunner()

VALID_COUNTS = {"read": 5, "edit": 6, "test": 3, "recovery": 3, "security": 3}


def _output(result) -> str:
    """Combine stdout and stderr: Click 8.2+ routes usage errors to stderr."""
    return result.output + (getattr(result, "stderr", "") or "")


def _make_task(task_id: str, category: str, **extra: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "id": task_id,
        "description": f"task {task_id}",
        "category": category,
        "message": f"do {task_id}",
        "fixture": "fixture",
        "oracle": {"file_checks": [{"type": "file_not_exists", "path": "__nope__"}]},
    }
    task.update(extra)
    return task


def _suite_tasks(counts: dict[str, int]) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    index = 0
    for category, count in counts.items():
        for _ in range(count):
            index += 1
            tasks.append(_make_task(f"task-{index}", category))
    return tasks


def _write_suite(
    tmp_path: Path,
    tasks: list[dict[str, Any]],
    *,
    suite: str = "golden",
    suite_version: str = "1",
) -> Path:
    (tmp_path / "fixture").mkdir(exist_ok=True)
    data = {
        "schema_version": 2,
        "suite": suite,
        "suite_version": suite_version,
        "tasks": tasks,
    }
    path = tmp_path / "suite.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _write_v1_tasks(tmp_path: Path) -> Path:
    (tmp_path / "fixture").mkdir(exist_ok=True)
    data = {
        "tasks": [
            {
                "id": "v1-demo",
                "description": "v1 task",
                "fixture": "fixture",
                "message": "do nothing",
                "max_iterations": 2,
                "checks": [{"type": "file_not_exists", "path": "__nope__"}],
            }
        ]
    }
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_eval_suite_runs_with_fake_provider(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BALE_PROVIDER", "fake")
    suite_path = _write_suite(tmp_path, _suite_tasks(VALID_COUNTS))

    result = runner.invoke(app, ["eval", "--allow-local", "--suite", str(suite_path)])

    assert result.exit_code == 0, result.output
    assert "category check failed" not in result.output
    assert "preflight failed" not in result.output


def test_eval_suite_rejects_wrong_category_counts(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BALE_PROVIDER", "fake")
    bad_counts = dict(VALID_COUNTS)
    bad_counts["read"] = 4  # one short
    suite_path = _write_suite(tmp_path, _suite_tasks(bad_counts))

    result = runner.invoke(app, ["eval", "--allow-local", "--suite", str(suite_path)])

    assert result.exit_code != 0
    assert "category check failed" in result.output
    assert "read=4" in result.output


def test_eval_suite_category_check_skippable(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BALE_PROVIDER", "fake")
    bad_counts = dict(VALID_COUNTS)
    bad_counts["read"] = 4
    suite_path = _write_suite(tmp_path, _suite_tasks(bad_counts))

    result = runner.invoke(
        app,
        [
            "eval",
            "--allow-local",
            "--suite",
            str(suite_path),
            "--no-category-check",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "category check failed" not in result.output


def test_eval_tasks_and_suite_are_mutually_exclusive(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("BALE_PROVIDER", "fake")
    suite_path = _write_suite(tmp_path, _suite_tasks(VALID_COUNTS))
    tasks_path = _write_v1_tasks(tmp_path)

    result = runner.invoke(
        app,
        ["eval", "--suite", str(suite_path), "--tasks", str(tasks_path)],
    )

    assert result.exit_code != 0
    assert "exactly one of --tasks or --suite" in _output(result)


def test_eval_requires_either_tasks_or_suite(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BALE_PROVIDER", "fake")

    result = runner.invoke(app, ["eval"])

    assert result.exit_code != 0
    assert "exactly one of --tasks or --suite" in _output(result)


def test_eval_preflight_failure_aborts_before_run(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BALE_PROVIDER", "fake")
    tasks = [_make_task("t1", "read", gold_patch="missing.patch")]
    suite_path = _write_suite(tmp_path, tasks)

    result = runner.invoke(
        app,
        ["eval", "--suite", str(suite_path), "--no-category-check"],
    )

    assert result.exit_code != 0
    assert "preflight failed" in result.output
    assert "gold_patch not found" in result.output


def test_eval_skip_preflight_runs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BALE_PROVIDER", "fake")
    tasks = [_make_task("t1", "read", gold_patch="missing.patch")]
    suite_path = _write_suite(tmp_path, tasks)

    result = runner.invoke(
        app,
        [
            "eval",
            "--allow-local",
            "--suite",
            str(suite_path),
            "--no-category-check",
            "--skip-preflight",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "preflight failed" not in result.output


def test_eval_tasks_v1_unchanged(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BALE_PROVIDER", "fake")
    tasks_path = _write_v1_tasks(tmp_path)

    result = runner.invoke(
        app,
        ["eval", "--allow-local", "--tasks", str(tasks_path)],
    )

    assert result.exit_code == 0, result.output
    assert "category check failed" not in result.output
    assert "preflight failed" not in result.output


def test_eval_json_includes_manifest(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BALE_PROVIDER", "fake")
    suite_path = _write_suite(tmp_path, _suite_tasks(VALID_COUNTS))
    json_path = tmp_path / "report.json"

    result = runner.invoke(
        app,
        [
            "eval",
            "--allow-local",
            "--suite",
            str(suite_path),
            "--json",
            str(json_path),
        ],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["report_schema_version"] == 1
    assert data["manifest"]["dataset"]["suite"] == "golden"
    assert data["manifest"]["task_count"] == 20
    assert len(data["manifest"]["dataset"]["content_sha256"]) == 64
    assert data["manifest"]["model"]["provider"] == "fake"


def test_eval_artifacts_dir_persists_evidence(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BALE_PROVIDER", "fake")
    suite_path = _write_suite(tmp_path, _suite_tasks(VALID_COUNTS))
    artifacts = tmp_path / "artifacts"

    result = runner.invoke(
        app,
        [
            "eval",
            "--allow-local",
            "--suite",
            str(suite_path),
            "--artifacts-dir",
            str(artifacts),
        ],
    )

    assert result.exit_code == 0, result.output
    manifest = json.loads((artifacts / "manifest.json").read_text(encoding="utf-8"))
    report = json.loads((artifacts / "report.json").read_text(encoding="utf-8"))
    assert report["manifest"]["run_id"] == manifest["run_id"]
    assert (artifacts / "report.md").is_file()
    assert (artifacts / "tasks" / "task-1" / "0" / "outcome.json").is_file()


def test_eval_repeat_runs_each_task_twice(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BALE_PROVIDER", "fake")
    suite_path = _write_suite(tmp_path, _suite_tasks(VALID_COUNTS))
    json_path = tmp_path / "repeat.json"

    result = runner.invoke(
        app,
        [
            "eval",
            "--allow-local",
            "--suite",
            str(suite_path),
            "--repeat",
            "2",
            "--json",
            str(json_path),
        ],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["total"] == 40
    assert data["manifest"]["repeat"] == 2
    assert data["metrics"]["pass_at_k"] == 1.0
    assert data["metrics"]["pass_at_1"] == 1.0
    repeat_indexes = {item["repeat_index"] for item in data["outcomes"]}
    assert repeat_indexes == {0, 1}


def test_eval_compare_to_reports_unchanged_and_incomparable(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("BALE_PROVIDER", "fake")
    suite_path = _write_suite(tmp_path, _suite_tasks(VALID_COUNTS))
    baseline_path = tmp_path / "baseline.json"

    first = runner.invoke(
        app,
        [
            "eval",
            "--allow-local",
            "--suite",
            str(suite_path),
            "--json",
            str(baseline_path),
        ],
    )
    assert first.exit_code == 0, first.output

    same = runner.invoke(
        app,
        [
            "eval",
            "--allow-local",
            "--suite",
            str(suite_path),
            "--compare-to",
            str(baseline_path),
        ],
    )
    assert same.exit_code == 0, same.output
    assert "comparison: unchanged" in same.output

    suite_data = json.loads(suite_path.read_text(encoding="utf-8"))
    suite_data["tasks"][0]["message"] += " changed"
    suite_path.write_text(json.dumps(suite_data), encoding="utf-8")

    drift = runner.invoke(
        app,
        [
            "eval",
            "--allow-local",
            "--suite",
            str(suite_path),
            "--compare-to",
            str(baseline_path),
        ],
    )
    assert drift.exit_code == 1
    assert "comparison: incomparable" in drift.output
    assert "dataset" in drift.output


def test_eval_treatment_requires_compare_to(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BALE_PROVIDER", "fake")
    suite_path = _write_suite(tmp_path, _suite_tasks(VALID_COUNTS))

    result = runner.invoke(
        app,
        [
            "eval",
            "--allow-local",
            "--suite",
            str(suite_path),
            "--treatment",
            "model",
        ],
    )

    assert result.exit_code != 0
    assert "--treatment requires --compare-to" in _output(result)


def test_eval_local_requires_allow_local(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BALE_PROVIDER", "fake")
    suite_path = _write_suite(tmp_path, _suite_tasks(VALID_COUNTS))

    result = runner.invoke(
        app, ["eval", "--suite", str(suite_path), "--sandbox", "local"]
    )

    assert result.exit_code != 0
    assert "--allow-local" in _output(result)


def test_eval_local_with_allow_local_runs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BALE_PROVIDER", "fake")
    suite_path = _write_suite(tmp_path, _suite_tasks(VALID_COUNTS))

    result = runner.invoke(
        app,
        [
            "eval",
            "--suite",
            str(suite_path),
            "--sandbox",
            "local",
            "--allow-local",
        ],
    )

    assert result.exit_code == 0, result.output


def test_eval_validate_only_skips_execution(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BALE_PROVIDER", "fake")
    suite_path = _write_suite(tmp_path, _suite_tasks(VALID_COUNTS))

    result = runner.invoke(app, ["eval", "--suite", str(suite_path), "--validate-only"])

    assert result.exit_code == 0, result.output
    assert "validate-only" in result.output
    assert "checks passed" in result.output


def test_eval_provider_override_switches_to_fake(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BALE_PROVIDER", "openai")
    monkeypatch.setenv("BALE_OPENAI_API_KEY", "dummy")
    monkeypatch.setenv("BALE_OPENAI_BASE_URL", "http://localhost:11434")
    suite_path = _write_suite(tmp_path, _suite_tasks(VALID_COUNTS))
    json_path = tmp_path / "report.json"

    result = runner.invoke(
        app,
        [
            "eval",
            "--allow-local",
            "--suite",
            str(suite_path),
            "--provider",
            "fake",
            "--json",
            str(json_path),
        ],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["manifest"]["model"]["provider"] == "fake"


def test_eval_freeze_baseline_invokes_freeze(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BALE_PROVIDER", "fake")
    suite_path = _write_suite(tmp_path, _suite_tasks(VALID_COUNTS))
    baseline_dir = tmp_path / "baselines"
    calls: dict[str, object] = {}

    def fake_freeze(report, name, **kwargs):  # noqa: ANN001
        calls["name"] = name
        calls["kwargs"] = kwargs
        return baseline_dir / f"{name}.json"

    monkeypatch.setattr("bale_harness.cli.freeze_baseline", fake_freeze)

    result = runner.invoke(
        app,
        [
            "eval",
            "--allow-local",
            "--suite",
            str(suite_path),
            "--freeze-baseline",
            "golden-v1",
            "--baseline-dir",
            str(baseline_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls["name"] == "golden-v1"
    assert calls["kwargs"]["supersede"] is False
    assert calls["kwargs"]["baseline_dir"] == baseline_dir


def test_eval_freeze_baseline_error_aborts(tmp_path: Path, monkeypatch) -> None:
    from bale_harness.eval.baseline import BaselineError

    monkeypatch.setenv("BALE_PROVIDER", "fake")
    suite_path = _write_suite(tmp_path, _suite_tasks(VALID_COUNTS))

    def fake_freeze(report, name, **kwargs):  # noqa: ANN001
        raise BaselineError("boom")

    monkeypatch.setattr("bale_harness.cli.freeze_baseline", fake_freeze)

    result = runner.invoke(
        app,
        [
            "eval",
            "--allow-local",
            "--suite",
            str(suite_path),
            "--freeze-baseline",
            "golden-v1",
        ],
    )

    assert result.exit_code != 0
    assert "boom" in result.output


def test_eval_compare_to_e2e(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BALE_PROVIDER", "fake")
    repo_root = Path(__file__).resolve().parents[1]
    tasks = repo_root / "examples" / "eval" / "tasks.json"
    base = tmp_path / "base.json"
    candidate = tmp_path / "candidate.json"

    first = runner.invoke(
        app,
        ["eval", "--allow-local", "--tasks", str(tasks), "--json", str(base)],
    )
    assert first.exit_code in (0, 1), first.output

    second = runner.invoke(
        app,
        [
            "eval",
            "--allow-local",
            "--tasks",
            str(tasks),
            "--compare-to",
            str(base),
            "--json",
            str(candidate),
        ],
    )
    assert second.exit_code in (0, 1), second.output

    data = json.loads(candidate.read_text(encoding="utf-8"))
    assert data["comparison"] is not None
    assert data["comparison"]["sample_size"] >= 1
