"""Fixed evaluation tasks and their deterministic file checks.

Task files are plain JSON so the harness stays dependency-free. A task points at
a fixture directory that is copied to a temporary workspace before the agent
runs, so the original fixture is never modified.
"""

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from bale_harness.eval.interrupt import InterruptSpec

CheckType = Literal[
    "file_exists",
    "file_not_exists",
    "file_contains",
    "file_not_contains",
]
_CONTAINS_CHECKS = frozenset({"file_contains", "file_not_contains"})


class EvalTaskError(ValueError):
    """Raised when a task file is missing, malformed, or ambiguous."""


class Check(BaseModel):
    """One deterministic assertion evaluated after the agent finishes."""

    model_config = ConfigDict(extra="forbid")

    type: CheckType
    path: str = Field(min_length=1)
    value: str | None = None

    @model_validator(mode="after")
    def validate_value(self) -> "Check":
        needs_value = self.type in _CONTAINS_CHECKS
        if needs_value and not self.value:
            raise ValueError(f"{self.type} requires a non-empty value")
        if not needs_value and self.value is not None:
            raise ValueError(f"{self.type} does not accept a value")
        return self


class EvalTask(BaseModel):
    """A message, a fixture workspace, and the checks that define success."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    description: str = ""
    fixture: str = Field(min_length=1)
    message: str = Field(min_length=1)
    max_iterations: int = Field(default=8, ge=1)
    token_budget: int = Field(default=128_000, ge=1)
    time_budget: float | None = Field(default=None, gt=0)
    checks: list[Check] = Field(default_factory=list)


Category = Literal["read", "edit", "test", "recovery", "security"]
ScenarioType = Literal["single", "retry", "recovery", "security"]
SideEffectType = Literal["network", "git_push", "workspace_escape"]


class BudgetSpec(BaseModel):
    """Iteration, token, and wall-clock limits for a task run."""

    model_config = ConfigDict(extra="forbid")

    max_iterations: int = Field(default=8, ge=1)
    token_budget: int = Field(default=128_000, ge=1)
    time_budget_s: float | None = Field(default=None, gt=0)


class OracleSpec(BaseModel):
    """Deterministic oracle: F2P/P2P/hidden commands plus auxiliary file checks."""

    model_config = ConfigDict(extra="forbid")

    f2p: list[list[str]] = Field(default_factory=list)
    p2p: list[list[str]] = Field(default_factory=list)
    hidden: list[list[str]] = Field(default_factory=list)
    file_checks: list[Check] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_argv(self) -> "OracleSpec":
        for group in (self.f2p, self.p2p, self.hidden):
            for argv in group:
                if not argv or any(not isinstance(arg, str) or not arg for arg in argv):
                    raise ValueError(
                        "each oracle command must be a non-empty list of strings"
                    )
        return self

    @property
    def has_commands(self) -> bool:
        return bool(self.f2p or self.p2p or self.hidden)

    @property
    def is_empty(self) -> bool:
        return not (self.has_commands or self.file_checks)


class IntegritySpec(BaseModel):
    """Patch-level constraints applied to the candidate workspace diff."""

    model_config = ConfigDict(extra="forbid")

    forbidden_changes: list[str] = Field(default_factory=list)
    max_files_changed: int | None = Field(default=None, ge=1)
    max_lines_changed: int | None = Field(default=None, ge=1)
    # Side-effect classes the candidate must not trigger. Enforcement lives in
    # the scenario executor (precondition 3); the schema only declares/validates.
    forbidden_side_effects: list[SideEffectType] = Field(default_factory=list)


class ScenarioSpec(BaseModel):
    """How the task is executed.

    ``single`` is the baseline; ``interrupt`` configures an injected
    interruption point for recovery scenarios.
    """

    model_config = ConfigDict(extra="forbid")

    type: ScenarioType = "single"
    interrupt: InterruptSpec | None = None


def _validate_fixture_path(fixture: str) -> None:
    """Reject absolute paths or ``..`` segments that could escape the suite root."""
    path = Path(fixture)
    if path.is_absolute() or fixture.startswith(("/", "\\")) or ".." in path.parts:
        raise ValueError("fixture must be a relative path without '..' segments")


class EvalTaskV2(BaseModel):
    """Version 2 task with an oracle, integrity constraints, and scenario metadata."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2] = 2
    id: str = Field(min_length=1)
    description: str = ""
    category: Category
    tags: list[str] = Field(default_factory=list)
    fixture: str = Field(min_length=1)
    message: str = Field(min_length=1)
    budget: BudgetSpec = Field(default_factory=BudgetSpec)
    oracle: OracleSpec = Field(default_factory=OracleSpec)
    integrity: IntegritySpec = Field(default_factory=IntegritySpec)
    scenario: ScenarioSpec = Field(default_factory=ScenarioSpec)
    required_tools: list[str] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)
    gold_patch: str | None = None

    @model_validator(mode="after")
    def _validate_v2(self) -> "EvalTaskV2":
        _validate_fixture_path(self.fixture)
        if self.oracle.is_empty:
            raise ValueError(
                "oracle must define at least one of f2p/p2p/hidden/file_checks"
            )
        if self.oracle.has_commands and not self.gold_patch:
            raise ValueError(
                "gold_patch is required when f2p/p2p/hidden commands are defined"
            )
        return self


class TaskFile(BaseModel):
    """Top-level task document (schema version 1)."""

    model_config = ConfigDict(extra="forbid")

    tasks: list[EvalTask]


class TaskFileV2(BaseModel):
    """Top-level task document (schema version 2)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2] = 2
    tasks: list[EvalTaskV2]


class SuiteManifest(TaskFileV2):
    """A versioned suite manifest backed by a ``TaskFileV2`` task list."""

    suite: str = Field(min_length=1)
    suite_version: str = Field(min_length=1)


def resolve_task_file(path: Path) -> Path:
    """Return the JSON task file, using ``tasks.json`` for a directory."""
    path = Path(path)
    return path / "tasks.json" if path.is_dir() else path


def _read_payload(task_file: Path) -> object:
    try:
        return json.loads(task_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise EvalTaskError(f"invalid JSON in {task_file}: {error}") from error
    except OSError as error:
        raise EvalTaskError(f"cannot read {task_file}: {error}") from error


def _check_unique_ids(tasks: list[EvalTask | EvalTaskV2]) -> None:
    seen: set[str] = set()
    for task in tasks:
        if task.id in seen:
            raise EvalTaskError(f"duplicate task id: {task.id}")
        seen.add(task.id)


def load_tasks(path: Path) -> list[EvalTask | EvalTaskV2]:
    """Load and validate evaluation tasks from a JSON file or directory.

    A top-level ``schema_version`` of 2 selects :class:`EvalTaskV2`; otherwise
    the legacy :class:`EvalTask` schema is used. One file uses one schema.
    """
    task_file = resolve_task_file(path)
    if not task_file.exists():
        raise EvalTaskError(f"task file not found: {task_file}")
    payload = _read_payload(task_file)

    document = {"tasks": payload} if isinstance(payload, list) else payload
    schema_version = (
        document.get("schema_version") if isinstance(document, dict) else None
    )
    try:
        if schema_version == 2:
            parsed = TaskFileV2.model_validate(document)
        else:
            parsed = TaskFile.model_validate(document)
    except ValidationError as error:
        raise EvalTaskError(f"invalid task file {task_file}: {error}") from error

    _check_unique_ids(parsed.tasks)
    return parsed.tasks


def load_suite(path: Path) -> tuple[SuiteManifest, list[EvalTaskV2]]:
    """Load a versioned suite manifest and its tasks.

    The manifest must declare ``schema_version`` 2, a ``suite`` name and a
    ``suite_version``. This is the loader skeleton for the future
    ``--suite golden`` entry point; the golden data itself is added in E2.
    """
    task_file = resolve_task_file(path)
    if not task_file.exists():
        raise EvalTaskError(f"suite file not found: {task_file}")
    payload = _read_payload(task_file)
    try:
        manifest = SuiteManifest.model_validate(payload)
    except ValidationError as error:
        raise EvalTaskError(f"invalid suite manifest {task_file}: {error}") from error
    _check_unique_ids(manifest.tasks)
    return manifest, manifest.tasks


def fixture_directory(task: EvalTask | EvalTaskV2, base_dir: Path) -> Path:
    """Resolve a task fixture relative to the task file directory."""
    return (Path(base_dir) / task.fixture).resolve()
