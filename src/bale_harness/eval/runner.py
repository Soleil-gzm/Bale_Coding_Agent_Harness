"""Deterministic evaluation runner over temporary fixture copies.

Each task runs in a fresh temporary workspace copied from its fixture, so the
evaluation is repeatable and never mutates ``examples/eval``. Writes and
commands are auto-approved because fixtures are trusted; a real Provider should
be configured through ``BALE_PROVIDER`` for meaningful task results.
"""

import shutil
import tempfile
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from bale_harness.agent.approval import auto_approve
from bale_harness.agent.loop import build_default_loop
from bale_harness.agent.security import WorkspaceError, WorkspaceGuard
from bale_harness.contracts import Turn
from bale_harness.eval.task import Check, EvalTask, fixture_directory
from bale_harness.providers import FakeProvider, ModelProvider

ProviderFactory = Callable[[EvalTask], ModelProvider]
OutcomeStatus = Literal["passed", "failed", "error"]


class CheckResult(BaseModel):
    """Outcome of one deterministic check."""

    model_config = ConfigDict(extra="forbid")

    check: Check
    passed: bool
    detail: str


class TaskOutcome(BaseModel):
    """Run status, usage, and check results for a single task."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    description: str = ""
    status: OutcomeStatus
    run_status: str | None = None
    answer: str | None = None
    error: str | None = None
    iterations: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    duration_ms: int = Field(default=0, ge=0)
    check_results: list[CheckResult] = Field(default_factory=list)


class EvalReport(BaseModel):
    """Aggregate evaluation report, JSON serializable for CI storage."""

    model_config = ConfigDict(extra="forbid")

    started_at: str
    duration_ms: int = Field(ge=0)
    total: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    errors: int = Field(ge=0)
    outcomes: list[TaskOutcome] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.failed == 0 and self.errors == 0


def _elapsed_ms(start: float) -> int:
    return max(0, int((time.perf_counter() - start) * 1000))


def evaluate_checks(workspace: Path, checks: Sequence[Check]) -> list[CheckResult]:
    """Evaluate file checks against ``workspace`` without raising on failure."""
    guard = WorkspaceGuard(workspace)
    results: list[CheckResult] = []
    for check in checks:
        try:
            target = guard.resolve(check.path)
        except WorkspaceError:
            results.append(
                CheckResult(check=check, passed=False, detail="path escapes workspace")
            )
            continue

        if check.type == "file_exists":
            passed = target.is_file()
            detail = "file exists" if passed else "file not found"
        elif check.type == "file_not_exists":
            passed = not target.exists()
            detail = "file absent" if passed else "file exists"
        else:
            passed, detail = _check_content(check, target)
        results.append(CheckResult(check=check, passed=passed, detail=detail))
    return results


def _check_content(check: Check, target: Path) -> tuple[bool, str]:
    if not target.is_file():
        return False, "file not found"
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        return False, f"cannot read file: {error}"
    value = check.value or ""
    present = value in text
    if check.type == "file_contains":
        return (True, f"found {value!r}") if present else (False, f"missing {value!r}")
    return (True, f"absent {value!r}") if not present else (False, f"found {value!r}")


async def run_task(
    task: EvalTask, fixture: Path, provider: ModelProvider
) -> TaskOutcome:
    """Copy ``fixture``, run one agent turn, and evaluate the task checks."""
    started = time.perf_counter()
    temp_root = Path(tempfile.mkdtemp(prefix="bale-eval-"))
    workspace = temp_root / "workspace"
    try:
        shutil.copytree(fixture, workspace)
        loop = build_default_loop(provider, workspace, approver=auto_approve)
        turn = Turn(
            message=task.message,
            workspace=workspace,
            token_budget=task.token_budget,
            max_iterations=task.max_iterations,
        )
        result = await loop.run(turn)
        check_results = evaluate_checks(workspace, task.checks)
        failed_checks = [item for item in check_results if not item.passed]
        if result.status != "completed":
            status: OutcomeStatus = "failed"
            error = result.error or f"run status {result.status}"
        elif failed_checks:
            status = "failed"
            error = f"{len(failed_checks)} check(s) failed"
        else:
            status = "passed"
            error = None
        return TaskOutcome(
            task_id=task.id,
            description=task.description,
            status=status,
            run_status=result.status,
            answer=result.answer,
            error=error,
            iterations=result.iterations,
            tool_calls=result.tool_calls,
            total_tokens=result.usage.total_tokens,
            duration_ms=_elapsed_ms(started),
            check_results=check_results,
        )
    except Exception as error:  # fixture or runtime failure becomes a task error
        return TaskOutcome(
            task_id=task.id,
            description=task.description,
            status="error",
            error=f"{type(error).__name__}: {error}",
            duration_ms=_elapsed_ms(started),
        )
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


async def run_tasks(
    tasks: Sequence[EvalTask],
    base_dir: Path,
    provider_factory: ProviderFactory | None = None,
) -> EvalReport:
    """Run tasks sequentially with a fresh provider and workspace per task."""
    factory: ProviderFactory = provider_factory or (lambda task: FakeProvider())
    started = time.perf_counter()
    started_at = datetime.now(UTC).isoformat()
    outcomes: list[TaskOutcome] = []
    for task in tasks:
        fixture = fixture_directory(task, base_dir)
        if not fixture.is_dir():
            outcomes.append(
                TaskOutcome(
                    task_id=task.id,
                    description=task.description,
                    status="error",
                    error=f"fixture not found: {fixture}",
                )
            )
            continue
        outcomes.append(await run_task(task, fixture, factory(task)))

    passed = sum(1 for outcome in outcomes if outcome.status == "passed")
    failed = sum(1 for outcome in outcomes if outcome.status == "failed")
    errors = sum(1 for outcome in outcomes if outcome.status == "error")
    return EvalReport(
        started_at=started_at,
        duration_ms=_elapsed_ms(started),
        total=len(outcomes),
        passed=passed,
        failed=failed,
        errors=errors,
        outcomes=outcomes,
    )
