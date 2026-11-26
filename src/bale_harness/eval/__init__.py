"""Fixed tasks, deterministic checks, and reports for offline evaluation."""

from bale_harness.eval.integrity import (
    IntegrityResult,
    check_integrity,
    snapshot,
)
from bale_harness.eval.report import render_markdown
from bale_harness.eval.runner import (
    CheckResult,
    EvalReport,
    TaskOutcome,
    evaluate_checks,
    run_task,
    run_tasks,
)
from bale_harness.eval.task import (
    Check,
    EvalTask,
    EvalTaskError,
    EvalTaskV2,
    IntegritySpec,
    OracleSpec,
    ScenarioSpec,
    SuiteManifest,
    fixture_directory,
    load_suite,
    load_tasks,
    resolve_task_file,
)

__all__ = [
    "Check",
    "CheckResult",
    "EvalReport",
    "EvalTask",
    "EvalTaskError",
    "EvalTaskV2",
    "IntegrityResult",
    "IntegritySpec",
    "OracleSpec",
    "ScenarioSpec",
    "SuiteManifest",
    "TaskOutcome",
    "check_integrity",
    "evaluate_checks",
    "fixture_directory",
    "load_suite",
    "load_tasks",
    "render_markdown",
    "resolve_task_file",
    "run_task",
    "run_tasks",
    "snapshot",
]
