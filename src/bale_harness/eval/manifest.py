"""Reproducible fingerprints for one evaluation run.

The manifest records the five effect-affecting inputs (model, prompt, tool
schema, sandbox, dataset) plus git state and feature flags. Hashes are computed
over canonical JSON and raw file bytes with posix paths, so two runs on the same
inputs produce identical fingerprints. Secrets are never recorded.
"""

import hashlib
import json
import platform
import subprocess
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from bale_harness.agent.context import SYSTEM_PROMPT
from bale_harness.agent.messages import ToolSpec
from bale_harness.agent.security import WorkspaceGuard
from bale_harness.agent.tools.builtin import build_default_tools
from bale_harness.eval.task import EvalTask, EvalTaskV2, fixture_directory
from bale_harness.settings import Settings

REPORT_SCHEMA_VERSION = 1

_IGNORED_PARTS = frozenset({"__pycache__", ".pytest_cache", ".git"})


class GitFingerprint(BaseModel):
    """Repository state at run time; ``None`` outside a git work tree."""

    model_config = ConfigDict(extra="forbid")

    commit: str | None = None
    dirty: bool | None = None


class ModelFingerprint(BaseModel):
    """Which model endpoint answered the tasks."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    base_url: str


class PromptFingerprint(BaseModel):
    """Hashes of the static system prompt and the task messages."""

    model_config = ConfigDict(extra="forbid")

    system_prompt_sha256: str
    messages_sha256: str


class ToolFingerprint(BaseModel):
    """Hash of the built-in tool schemas exposed to the model."""

    model_config = ConfigDict(extra="forbid")

    schema_sha256: str
    names: list[str] = Field(default_factory=list)


class SandboxFingerprint(BaseModel):
    """Sandbox backend and resource limits used for task commands."""

    model_config = ConfigDict(extra="forbid")

    backend: str
    image: str
    network: bool
    memory: str
    cpus: float
    pids_limit: int
    user: str
    image_digest: str | None = None


class DatasetFingerprint(BaseModel):
    """Identity of the task suite and of every input file it references."""

    model_config = ConfigDict(extra="forbid")

    suite: str
    suite_version: str
    content_sha256: str
    file_count: int = Field(default=0, ge=0)


class EvalManifest(BaseModel):
    """Everything needed to decide whether two reports are comparable."""

    model_config = ConfigDict(extra="forbid")

    report_schema_version: int = REPORT_SCHEMA_VERSION
    run_id: str
    created_at: str
    task_count: int = Field(ge=0)
    repeat: int = Field(default=1, ge=1)
    git: GitFingerprint = Field(default_factory=GitFingerprint)
    model: ModelFingerprint
    prompt: PromptFingerprint
    tools: ToolFingerprint
    sandbox: SandboxFingerprint
    dataset: DatasetFingerprint
    feature_flags: dict[str, str | int | bool | float] = Field(default_factory=dict)

    def comparability_fields(self) -> dict[str, str]:
        """Flatten the fields a baseline comparison must match."""
        return {
            "report_schema_version": str(self.report_schema_version),
            "dataset": self.dataset.content_sha256,
            "model": f"{self.model.provider}:{self.model.model}@{self.model.base_url}",
            "prompt": (
                f"{self.prompt.system_prompt_sha256}:{self.prompt.messages_sha256}"
            ),
            "tools": self.tools.schema_sha256,
            "sandbox": (
                f"{self.sandbox.backend}:{self.sandbox.image}:"
                f"{self.sandbox.network}:{self.sandbox.memory}:"
                f"{self.sandbox.cpus}:{self.sandbox.pids_limit}:{self.sandbox.user}"
            ),
        }


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _sha256_json(payload: object) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return _sha256_bytes(encoded.encode("utf-8"))


def _iter_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    files = [
        path
        for path in root.rglob("*")
        if path.is_file() and not _IGNORED_PARTS.intersection(path.parts)
    ]
    return sorted(files, key=lambda path: path.as_posix())


def _dataset_entries(
    tasks: Sequence[EvalTask | EvalTaskV2], base_dir: Path
) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for task in tasks:
        roots = [fixture_directory(task, base_dir), base_dir / "verifier" / task.id]
        if isinstance(task, EvalTaskV2) and task.gold_patch:
            roots.append(base_dir / task.gold_patch)
        for root in roots:
            candidates = [root] if root.is_file() else _iter_files(root)
            for path in candidates:
                entries.append(
                    {
                        "path": path.relative_to(base_dir).as_posix(),
                        "sha256": _sha256_file(path),
                    }
                )
    return entries


def hash_dataset(
    tasks: Sequence[EvalTask | EvalTaskV2], base_dir: Path
) -> DatasetFingerprint:
    """Hash task definitions plus every fixture, verifier, and patch file."""
    entries = _dataset_entries(tasks, base_dir)
    payload = {
        "tasks": [task.model_dump(mode="json") for task in tasks],
        "files": entries,
    }
    return DatasetFingerprint(
        suite="adhoc",
        suite_version="0",
        content_sha256=_sha256_json(payload),
        file_count=len(entries),
    )


def _prompt_fingerprint(tasks: Sequence[EvalTask | EvalTaskV2]) -> PromptFingerprint:
    return PromptFingerprint(
        system_prompt_sha256=_sha256_bytes(SYSTEM_PROMPT.encode("utf-8")),
        messages_sha256=_sha256_json([task.message for task in tasks]),
    )


def _tool_fingerprint() -> ToolFingerprint:
    guard = WorkspaceGuard(Path(tempfile.gettempdir()))
    specs: list[ToolSpec] = sorted(
        (tool.spec for tool in build_default_tools(guard)),
        key=lambda spec: spec.name,
    )
    payload = [spec.model_dump(mode="json") for spec in specs]
    return ToolFingerprint(
        schema_sha256=_sha256_json(payload),
        names=[spec.name for spec in specs],
    )


def _git_fingerprint(base_dir: Path) -> GitFingerprint:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=base_dir,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain", "--", "."],
            cwd=base_dir,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return GitFingerprint()
    return GitFingerprint(commit=commit or None, dirty=bool(status))


def _sandbox_fingerprint(
    settings: Settings, backend: str, image: str
) -> SandboxFingerprint:
    return SandboxFingerprint(
        backend=backend,
        image=image,
        network=settings.sandbox_network,
        memory=settings.sandbox_memory,
        cpus=settings.sandbox_cpus,
        pids_limit=settings.sandbox_pids_limit,
        user=settings.sandbox_user,
    )


def _model_fingerprint(settings: Settings) -> ModelFingerprint:
    if settings.provider == "fake":
        return ModelFingerprint(provider="fake", model="fake", base_url="")
    return ModelFingerprint(
        provider=settings.provider,
        model=settings.openai_model,
        base_url=settings.openai_base_url,
    )


def _feature_flags(settings: Settings) -> dict[str, str | int | bool | float]:
    return {
        "retrieval_enabled": settings.retrieval_enabled,
        "retrieval_top_k": settings.retrieval_top_k,
        "retrieval_hybrid": settings.retrieval_hybrid,
        "retrieval_alpha": settings.retrieval_alpha,
        "trace_enabled": settings.trace_path is not None,
        "cost_tracking": (
            settings.input_cost_per_1k is not None
            or settings.output_cost_per_1k is not None
        ),
        "mcp_tools": "excluded",
        "python": platform.python_version(),
    }


def build_manifest(
    tasks: Sequence[EvalTask | EvalTaskV2],
    base_dir: Path,
    settings: Settings,
    *,
    sandbox_backend: str | None = None,
    sandbox_image: str | None = None,
    suite: str | None = None,
    suite_version: str | None = None,
    repeat: int = 1,
    run_id: str | None = None,
) -> EvalManifest:
    """Assemble the fingerprint set for a run over ``tasks`` under ``base_dir``."""
    dataset = hash_dataset(tasks, base_dir)
    if suite is not None:
        dataset = dataset.model_copy(update={"suite": suite})
    if suite_version is not None:
        dataset = dataset.model_copy(update={"suite_version": suite_version})
    backend = sandbox_backend or settings.sandbox_backend
    image = sandbox_image or settings.sandbox_image
    resolved_run_id = run_id or (
        datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + dataset.content_sha256[:8]
    )
    return EvalManifest(
        run_id=resolved_run_id,
        created_at=datetime.now(UTC).isoformat(),
        task_count=len(tasks),
        repeat=repeat,
        git=_git_fingerprint(base_dir),
        model=_model_fingerprint(settings),
        prompt=_prompt_fingerprint(tasks),
        tools=_tool_fingerprint(),
        sandbox=_sandbox_fingerprint(settings, backend, image),
        dataset=dataset,
        feature_flags=_feature_flags(settings),
    )
