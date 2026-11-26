"""Fixed tasks, deterministic checks, and reports for offline evaluation."""

from bale_harness.eval.integrity import (
    IntegrityResult,
    check_integrity,
    snapshot,
)
from bale_harness.eval.interrupt import InterruptController, InterruptSpec
from bale_harness.eval.oracle import (
    OracleError,
    OracleEvaluation,
    OracleGroupResult,
    evaluate_oracle,
    parse_junit,
)
from bale_harness.eval.report import render_markdown
from bale_harness.eval.runner import (
    CheckResult,
    EvalReport,
    PreflightResult,
    TaskOutcome,
    evaluate_checks,
    preflight_task,
    run_task,
    run_tasks,
)
from bale_harness.eval.scenarios import ScenarioResult, run_scenario
from bale_harness.eval.task import (
    BudgetSpec,
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
    "BudgetSpec",
    "Check",
    "CheckResult",
    "EvalReport",
    "EvalTask",
    "EvalTaskError",
    "EvalTaskV2",
    "IntegrityResult",
    "IntegritySpec",
    "InterruptController",
    "InterruptSpec",
    "OracleError",
    "OracleEvaluation",
    "OracleGroupResult",
    "OracleSpec",
    "PreflightResult",
    "ScenarioResult",
    "ScenarioSpec",
    "SuiteManifest",
    "TaskOutcome",
    "check_integrity",
    "evaluate_checks",
    "evaluate_oracle",
    "parse_junit",
    "preflight_task",
    "fixture_directory",
    "load_suite",
    "load_tasks",
    "render_markdown",
    "resolve_task_file",
    "run_scenario",
    "run_task",
    "run_tasks",
    "snapshot",
]
