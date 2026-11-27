"""Deterministic evaluation runner over temporary fixture copies.

Each task runs in a fresh temporary workspace copied from its fixture, so the
evaluation is repeatable and never mutates ``examples/eval``. Writes and
commands are auto-approved because fixtures are trusted; a real Provider should
be configured through ``BALE_PROVIDER`` for meaningful task results. A
``sandbox_factory`` can isolate task commands, for example in Docker.

Version 2 tasks use three isolated workspaces: a pristine reference snapshot,
the agent workspace that the model edits, and a verification copy that is
checked for integrity, restored for protected paths, and evaluated by the
oracle (F2P/P2P/hidden). Version 1 tasks keep the original single-workspace
behaviour so existing reports are unchanged.
"""

import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from bale_harness.agent.approval import auto_approve
from bale_harness.agent.loop import build_default_loop
from bale_harness.agent.sandbox import Sandbox
from bale_harness.agent.security import WorkspaceError, WorkspaceGuard
from bale_harness.contracts import Turn
from bale_harness.eval.evidence import EvidenceLayout, write_task_evidence
from bale_harness.eval.integrity import IntegrityResult, check_integrity, render_diff
from bale_harness.eval.manifest import EvalManifest
from bale_harness.eval.oracle import OracleEvaluation, build_eval_env, evaluate_oracle
from bale_harness.eval.scenarios import run_scenario
from bale_harness.eval.task import Check, EvalTask, EvalTaskV2, fixture_directory
from bale_harness.providers import FakeProvider, ModelProvider

ProviderFactory = Callable[[EvalTask | EvalTaskV2], ModelProvider]
SandboxFactory = Callable[[Path], Sandbox]
OutcomeStatus = Literal["passed", "failed", "error"]


class CheckResult(BaseModel):
    """Outcome of one deterministic check."""

    model_config = ConfigDict(extra="forbid")

    check: Check
    passed: bool
    detail: str


class ViolationOutcome(BaseModel):
    """A single integrity violation, serializable in the task report."""

    model_config = ConfigDict(extra="forbid")

    code: str
    path: str
    detail: str


class IntegrityOutcome(BaseModel):
    """Summary of the candidate-vs-pristine integrity check."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    files_changed: int = 0
    lines_added: int = 0
    lines_removed: int = 0
    added: list[str] = Field(default_factory=list)
    modified: list[str] = Field(default_factory=list)
    deleted: list[str] = Field(default_factory=list)
    violations: list[ViolationOutcome] = Field(default_factory=list)


class OracleGroupOutcome(BaseModel):
    """Summary of one oracle group (f2p/p2p/hidden)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    status: str
    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    missing: int = 0
    error: str | None = None


class OracleOutcome(BaseModel):
    """Summary of the full oracle evaluation for one task."""

    model_config = ConfigDict(extra="forbid")

    all_passed: bool
    has_error: bool
    f2p: OracleGroupOutcome
    p2p: OracleGroupOutcome
    hidden: OracleGroupOutcome


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
    oracle: OracleOutcome | None = None
    integrity: IntegrityOutcome | None = None


class EvalReport(BaseModel):
    """Aggregate evaluation report, JSON serializable for CI storage."""

    model_config = ConfigDict(extra="forbid")

    report_schema_version: int = 1
    started_at: str
    duration_ms: int = Field(ge=0)
    total: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    errors: int = Field(ge=0)
    outcomes: list[TaskOutcome] = Field(default_factory=list)
    manifest: EvalManifest | None = None

    @property
    def ok(self) -> bool:
        return self.failed == 0 and self.errors == 0


class PreflightResult(BaseModel):
    """Data-quality verdict for a single task before any model runs."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    ok: bool
    pristine_f2p_failed: bool = False
    pristine_p2p_passed: bool = False
    gold_all_passed: bool = False
    error: str | None = None


def _elapsed_ms(start: float) -> int:
    return max(0, int((time.perf_counter() - start) * 1000))


def _to_integrity_outcome(result: IntegrityResult) -> IntegrityOutcome:
    return IntegrityOutcome(
        ok=result.ok,
        files_changed=result.diff.files_changed,
        lines_added=result.diff.lines_added,
        lines_removed=result.diff.lines_removed,
        added=list(result.diff.added),
        modified=list(result.diff.modified),
        deleted=list(result.diff.deleted),
        violations=[
            ViolationOutcome(code=v.code, path=v.path, detail=v.detail)
            for v in result.violations
        ],
    )


def _to_oracle_outcome(evaluation: OracleEvaluation) -> OracleOutcome:
    def group(g: object) -> OracleGroupOutcome:
        return OracleGroupOutcome(
            name=g.name,
            status=g.status,
            total=g.total,
            passed=g.passed,
            failed=g.failed,
            skipped=g.skipped,
            missing=g.missing,
            error=g.error,
        )

    return OracleOutcome(
        all_passed=evaluation.all_passed,
        has_error=evaluation.has_error,
        f2p=group(evaluation.f2p),
        p2p=group(evaluation.p2p),
        hidden=group(evaluation.hidden),
    )


def _oracle_error_text(evaluation: OracleEvaluation) -> str:
    parts = [
        f"{g.name}: {g.error}"
        for g in (evaluation.f2p, evaluation.p2p, evaluation.hidden)
        if g.status == "error" and g.error
    ]
    return "; ".join(parts) or "oracle infrastructure error"


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


def _copy_tree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def _matching_files(root: Path, patterns: list[str]) -> list[str]:
    matched: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if any(PurePosixPath(rel).match(pattern) for pattern in patterns):
            matched.append(rel)
    return matched


def _restore_forbidden(pristine: Path, verification: Path, patterns: list[str]) -> None:
    """Overwrite verification files matching ``patterns`` with pristine copies.

    This lets the oracle run against unmodified protected tests; it is an
    evidence-collection step. Integrity still fails the task if the candidate
    touched any protected path.
    """
    for rel in _matching_files(pristine, patterns):
        src = pristine / rel
        dst = verification / rel
        if src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


def _inject_verifier(base_dir: Path, task_id: str, target: Path) -> None:
    """Copy task-private verifier files (e.g. hidden tests) into a run copy.

    Verifier files live outside the fixture so the agent never sees them; they
    are copied into the verification/pristine/gold copy right before the oracle
    runs. A missing verifier directory is a no-op (not every task has one).
    """
    verifier_dir = Path(base_dir) / "verifier" / task_id
    if not verifier_dir.is_dir():
        return
    for src in sorted(verifier_dir.rglob("*")):
        if not src.is_file():
            continue
        rel = src.relative_to(verifier_dir)
        dst = Path(target) / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


async def run_task(
    task: EvalTask | EvalTaskV2,
    fixture: Path,
    provider: ModelProvider,
    sandbox_factory: SandboxFactory | None = None,
    base_dir: Path | None = None,
    evidence_dir: Path | None = None,
) -> TaskOutcome:
    """Run one task; v2 tasks use the three-workspace integrity/oracle flow."""
    if isinstance(task, EvalTaskV2):
        return await _run_v2_task(
            task, fixture, provider, sandbox_factory, base_dir, evidence_dir
        )
    return await _run_v1_task(task, fixture, provider, sandbox_factory, evidence_dir)


async def _run_v1_task(
    task: EvalTask,
    fixture: Path,
    provider: ModelProvider,
    sandbox_factory: SandboxFactory | None,
    evidence_dir: Path | None = None,
) -> TaskOutcome:
    """Legacy single-workspace execution: copy fixture, run agent, check files."""
    started = time.perf_counter()
    temp_root = Path(tempfile.mkdtemp(prefix="bale-eval-"))
    workspace = temp_root / "workspace"
    try:
        shutil.copytree(fixture, workspace)
        loop = build_default_loop(
            provider,
            workspace,
            approver=auto_approve,
            sandbox=sandbox_factory(workspace) if sandbox_factory else None,
        )
        turn = Turn(
            message=task.message,
            workspace=workspace,
            token_budget=task.token_budget,
            max_iterations=task.max_iterations,
            time_budget=task.time_budget,
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
        outcome = TaskOutcome(
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
        if evidence_dir is not None:
            write_task_evidence(evidence_dir, outcome)
        return outcome
    except Exception as error:  # fixture or runtime failure becomes a task error
        outcome = TaskOutcome(
            task_id=task.id,
            description=task.description,
            status="error",
            error=f"{type(error).__name__}: {error}",
            duration_ms=_elapsed_ms(started),
        )
        if evidence_dir is not None:
            write_task_evidence(evidence_dir, outcome)
        return outcome
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


async def _run_v2_task(
    task: EvalTaskV2,
    fixture: Path,
    provider: ModelProvider,
    sandbox_factory: SandboxFactory | None,
    base_dir: Path | None = None,
    evidence_dir: Path | None = None,
) -> TaskOutcome:
    """Three-workspace execution: pristine diff, integrity gate, oracle verify."""
    started = time.perf_counter()
    temp_root = Path(tempfile.mkdtemp(prefix="bale-eval-"))
    pristine = temp_root / "pristine"
    workspace = temp_root / "workspace"
    verification = temp_root / "verification"
    evidence = evidence_dir or (temp_root / "evidence")
    if evidence_dir is not None:
        evidence_dir.mkdir(parents=True, exist_ok=True)
    try:
        _copy_tree(fixture, pristine)
        _copy_tree(fixture, workspace)
        env = build_eval_env()

        scenario_result = await run_scenario(task, workspace, provider, sandbox_factory)

        integrity_result = check_integrity(pristine, workspace, task.integrity)
        integrity_outcome = _to_integrity_outcome(integrity_result)

        _copy_tree(workspace, verification)
        _restore_forbidden(pristine, verification, task.integrity.forbidden_changes)
        if base_dir is not None:
            _inject_verifier(base_dir, task.id, verification)
        check_results = evaluate_checks(verification, task.oracle.file_checks)

        oracle_eval = evaluate_oracle(task.oracle, verification, evidence, env=env)
        oracle_outcome = _to_oracle_outcome(oracle_eval)

        failed_checks = [item for item in check_results if not item.passed]
        if scenario_result.run_status != "completed":
            status: OutcomeStatus = "failed"
            error = scenario_result.error or (
                f"run status {scenario_result.run_status}"
            )
        elif scenario_result.contract_violations:
            status = "failed"
            error = "; ".join(scenario_result.contract_violations)
        elif not integrity_result.ok:
            status = "failed"
            error = f"{len(integrity_result.violations)} integrity violation(s)"
        elif failed_checks:
            status = "failed"
            error = f"{len(failed_checks)} check(s) failed"
        elif oracle_eval.has_error:
            status = "error"
            error = _oracle_error_text(oracle_eval)
        elif not oracle_eval.all_passed:
            status = "failed"
            error = "oracle checks failed"
        else:
            status = "passed"
            error = None

        outcome = TaskOutcome(
            task_id=task.id,
            description=task.description,
            status=status,
            run_status=scenario_result.run_status,
            answer=scenario_result.answer,
            error=error,
            iterations=scenario_result.iterations,
            tool_calls=scenario_result.tool_calls,
            total_tokens=scenario_result.total_tokens,
            duration_ms=_elapsed_ms(started),
            check_results=check_results,
            oracle=oracle_outcome,
            integrity=integrity_outcome,
        )
        if evidence_dir is not None:
            write_task_evidence(
                evidence_dir,
                outcome,
                diff=render_diff(pristine, workspace),
            )
        return outcome
    except Exception as error:  # fixture or runtime failure becomes a task error
        outcome = TaskOutcome(
            task_id=task.id,
            description=task.description,
            status="error",
            error=f"{type(error).__name__}: {error}",
            duration_ms=_elapsed_ms(started),
        )
        if evidence_dir is not None:
            write_task_evidence(evidence_dir, outcome)
        return outcome
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def _apply_gold_patch(target: Path, patch: Path) -> None:
    """Apply a unified-diff gold patch in place using ``git apply``."""
    completed = subprocess.run(
        ["git", "apply", "--whitespace=nowarn", str(patch)],
        cwd=str(target),
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "git apply failed")


def preflight_task(
    task: EvalTaskV2,
    base_dir: Path,
    env: dict[str, str] | None = None,
    timeout_s: float = 60.0,
) -> PreflightResult:
    """Validate task data quality before any model is run.

    Confirms that F2P fails on the pristine fixture, P2P passes on pristine,
    and applying ``gold_patch`` makes every oracle group pass. This function
    is provided for suite authors and CI; it is not invoked automatically by
    :func:`run_tasks`.
    """
    if not task.gold_patch:
        return PreflightResult(
            task_id=task.id, ok=False, error="gold_patch is required for preflight"
        )
    gold_path = Path(base_dir) / task.gold_patch
    if not gold_path.is_file():
        return PreflightResult(
            task_id=task.id,
            ok=False,
            error=f"gold_patch not found: {gold_path}",
        )
    fixture = fixture_directory(task, base_dir)
    if not fixture.is_dir():
        return PreflightResult(
            task_id=task.id, ok=False, error=f"fixture not found: {fixture}"
        )

    run_env = env if env is not None else build_eval_env()
    temp_root = Path(tempfile.mkdtemp(prefix="bale-preflight-"))
    try:
        pristine = temp_root / "pristine"
        gold = temp_root / "gold"
        evidence = temp_root / "evidence"
        pristine.mkdir()
        gold.mkdir()
        evidence.mkdir()
        _copy_tree(fixture, pristine)
        _copy_tree(fixture, gold)
        try:
            _apply_gold_patch(gold, gold_path)
        except RuntimeError as error:
            return PreflightResult(
                task_id=task.id,
                ok=False,
                error=f"cannot apply gold_patch: {error}",
            )

        _inject_verifier(base_dir, task.id, pristine)
        _inject_verifier(base_dir, task.id, gold)
        pristine_eval = evaluate_oracle(
            task.oracle, pristine, evidence / "pristine", run_env, timeout_s
        )
        gold_eval = evaluate_oracle(
            task.oracle, gold, evidence / "gold", run_env, timeout_s
        )

        f2p_failed = pristine_eval.f2p.status == "failed"
        p2p_passed = pristine_eval.p2p.status == "passed"
        gold_passed = gold_eval.all_passed
        ok = f2p_failed and p2p_passed and gold_passed

        error: str | None = None
        if not ok:
            reasons = []
            if not f2p_failed:
                reasons.append("f2p does not fail on pristine")
            if not p2p_passed:
                reasons.append("p2p does not pass on pristine")
            if not gold_passed:
                reasons.append("gold patch does not pass all oracle groups")
            error = "; ".join(reasons)

        return PreflightResult(
            task_id=task.id,
            ok=ok,
            pristine_f2p_failed=f2p_failed,
            pristine_p2p_passed=p2p_passed,
            gold_all_passed=gold_passed,
            error=error,
        )
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


async def run_tasks(
    tasks: Sequence[EvalTask | EvalTaskV2],
    base_dir: Path,
    provider_factory: ProviderFactory | None = None,
    sandbox_factory: SandboxFactory | None = None,
    evidence_root: Path | None = None,
) -> EvalReport:
    """Run tasks sequentially with a fresh provider and workspace per task."""
    factory: ProviderFactory = provider_factory or (lambda task: FakeProvider())
    layout = EvidenceLayout(evidence_root) if evidence_root is not None else None
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
        evidence_dir = layout.task_dir(task.id) if layout is not None else None
        outcomes.append(
            await run_task(
                task,
                fixture,
                factory(task),
                sandbox_factory,
                base_dir,
                evidence_dir,
            )
        )

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
