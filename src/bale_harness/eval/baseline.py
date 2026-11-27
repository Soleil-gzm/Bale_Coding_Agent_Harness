"""Freeze named evaluation baselines under a baseline directory.

Baselines are frozen, immutable reports that serve as the reference for paired
comparisons (see docs/evaluation/design.md §10). A frozen baseline cannot be
overwritten: freezing an existing name fails unless ``supersede`` is True, in
which case the previous report is preserved as ``<name>.superseded.json`` with
the supersede reason stamped into its manifest, and the new report takes its
place. Official baselines must be hermetic and from a clean git tree
(design.md §10.2 rule 7); non-hermetic or dirty reports can still be frozen for
local debugging by passing ``require_hermetic=False``.
"""

from __future__ import annotations

import re
from pathlib import Path

from bale_harness.eval.runner import EvalReport

DEFAULT_BASELINE_DIR = Path("docs/evaluation/baselines")

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class BaselineError(RuntimeError):
    """Raised when a baseline cannot be frozen or read."""


def validate_baseline_name(name: str) -> None:
    """Reject names that could escape the baseline directory."""
    if not _NAME_RE.match(name) or name in {".", ".."}:
        raise BaselineError(f"invalid baseline name: {name!r}")


def baseline_path(baseline_dir: Path, name: str) -> Path:
    validate_baseline_name(name)
    return baseline_dir / f"{name}.json"


def _supersede_path(baseline_dir: Path, name: str) -> Path:
    return baseline_dir / f"{name}.superseded.json"


def _assert_freezeable(report: EvalReport, *, require_hermetic: bool) -> None:
    if not require_hermetic:
        return
    manifest = report.manifest
    if manifest is None:
        raise BaselineError(
            "report has no manifest; cannot freeze as an official baseline"
        )
    if not manifest.hermetic:
        raise BaselineError(
            "report is not hermetic; official baselines require a hermetic run "
            "(docker sandbox, network disabled, clean git tree)"
        )
    if manifest.git.dirty is not False:
        raise BaselineError(
            "report git tree is dirty or unknown; official baselines require a "
            "clean commit"
        )


def _mark_superseded(report: EvalReport, reason: str) -> EvalReport:
    manifest = report.manifest
    if manifest is None:
        return report
    flags = dict(manifest.feature_flags)
    flags["baseline_superseded"] = True
    flags["baseline_superseded_reason"] = reason
    stamped = manifest.model_copy(update={"feature_flags": flags})
    return report.model_copy(update={"manifest": stamped})


def freeze_baseline(
    report: EvalReport,
    name: str,
    *,
    baseline_dir: Path = DEFAULT_BASELINE_DIR,
    supersede: bool = False,
    supersede_reason: str | None = None,
    require_hermetic: bool = True,
) -> Path:
    """Freeze ``report`` as the named baseline and return the written path.

    Raises ``BaselineError`` when the name already exists and ``supersede`` is
    False, when ``supersede`` is requested without a ``supersede_reason``, or
    when the report fails the official-baseline hermetic/clean checks.
    """
    _assert_freezeable(report, require_hermetic=require_hermetic)
    path = baseline_path(baseline_dir, name)
    baseline_dir.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if not supersede:
            raise BaselineError(
                f"baseline {name!r} already exists at {path}; pass supersede=True "
                "to replace it"
            )
        if supersede_reason is None or not supersede_reason.strip():
            raise BaselineError("supersede requires a non-blank supersede_reason")
        previous = EvalReport.model_validate_json(path.read_text(encoding="utf-8"))
        archive = _supersede_path(baseline_dir, name)
        archive.write_text(
            _mark_superseded(previous, supersede_reason).model_dump_json(
                indent=2, exclude_none=True
            ),
            encoding="utf-8",
        )
    path.write_text(
        report.model_dump_json(indent=2, exclude_none=True), encoding="utf-8"
    )
    return path
