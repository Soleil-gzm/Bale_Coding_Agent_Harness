"""Fixed evaluation tasks and their deterministic file checks.

Task files are plain JSON so the harness stays dependency-free. A task points at
a fixture directory that is copied to a temporary workspace before the agent
runs, so the original fixture is never modified.
"""

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

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


class TaskFile(BaseModel):
    """Top-level task document."""

    model_config = ConfigDict(extra="forbid")

    tasks: list[EvalTask]


def resolve_task_file(path: Path) -> Path:
    """Return the JSON task file, using ``tasks.json`` for a directory."""
    path = Path(path)
    return path / "tasks.json" if path.is_dir() else path


def load_tasks(path: Path) -> list[EvalTask]:
    """Load and validate evaluation tasks from a JSON file or directory."""
    task_file = resolve_task_file(path)
    if not task_file.exists():
        raise EvalTaskError(f"task file not found: {task_file}")
    try:
        payload = json.loads(task_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise EvalTaskError(f"invalid JSON in {task_file}: {error}") from error
    except OSError as error:
        raise EvalTaskError(f"cannot read {task_file}: {error}") from error

    document = {"tasks": payload} if isinstance(payload, list) else payload
    try:
        task_file_model = TaskFile.model_validate(document)
    except ValidationError as error:
        raise EvalTaskError(f"invalid task file {task_file}: {error}") from error

    seen: set[str] = set()
    for task in task_file_model.tasks:
        if task.id in seen:
            raise EvalTaskError(f"duplicate task id: {task.id}")
        seen.add(task.id)
    return task_file_model.tasks


def fixture_directory(task: EvalTask, base_dir: Path) -> Path:
    """Resolve a task fixture relative to the task file directory."""
    return (Path(base_dir) / task.fixture).resolve()
