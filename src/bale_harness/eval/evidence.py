"""Persisted evidence for one evaluation run.

Layout::

    <root>/manifest.json
    <root>/report.json
    <root>/report.md
    <root>/tasks/<task_id>/<repeat_index>/outcome.json
                                              /integrity.json
                                              /candidate.diff
                                              /f2p_0.log, f2p_0.xml, ...

Oracle logs and JUnit files are written by ``evaluate_oracle`` directly into the
task directory; this module writes the outcome, integrity summary, and the
candidate diff.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bale_harness.eval.manifest import EvalManifest
    from bale_harness.eval.runner import EvalReport, TaskOutcome


class EvidenceLayout:
    """Path derivation for an evidence directory."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.json"

    @property
    def report_json_path(self) -> Path:
        return self.root / "report.json"

    @property
    def report_md_path(self) -> Path:
        return self.root / "report.md"

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def task_dir(self, task_id: str, repeat_index: int = 0) -> Path:
        return self.root / "tasks" / task_id / str(repeat_index)


def write_manifest(layout: EvidenceLayout, manifest: EvalManifest) -> None:
    """Write ``manifest.json`` for the run."""
    layout.ensure()
    layout.manifest_path.write_text(
        manifest.model_dump_json(indent=2), encoding="utf-8"
    )


def write_report(layout: EvidenceLayout, report: EvalReport, markdown: str) -> None:
    """Write ``report.json`` and ``report.md`` for the run."""
    layout.ensure()
    layout.report_json_path.write_text(
        report.model_dump_json(indent=2, exclude_none=True), encoding="utf-8"
    )
    layout.report_md_path.write_text(markdown, encoding="utf-8")


def write_task_evidence(
    evidence_dir: Path,
    outcome: TaskOutcome,
    *,
    diff: str | None = None,
) -> None:
    """Write the outcome, integrity summary, and candidate diff for one task."""
    evidence_dir = Path(evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "outcome.json").write_text(
        outcome.model_dump_json(indent=2, exclude_none=True), encoding="utf-8"
    )
    if outcome.integrity is not None:
        (evidence_dir / "integrity.json").write_text(
            outcome.integrity.model_dump_json(indent=2, exclude_none=True),
            encoding="utf-8",
        )
    if diff is not None:
        (evidence_dir / "candidate.diff").write_text(diff, encoding="utf-8")
