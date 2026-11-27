"""Evidence persistence tests for the evaluation runner (E3)."""

import asyncio
import difflib
import json
import sys
from pathlib import Path

from bale_harness.agent.messages import ModelResponse, ToolCall
from bale_harness.eval import (
    EvalReport,
    EvalTask,
    EvalTaskV2,
    EvidenceLayout,
    build_manifest,
    render_diff,
    run_tasks,
)
from bale_harness.providers import FakeProvider
from bale_harness.settings import Settings

PY = [sys.executable]

BUGGY = "def add(a, b):\n    return a + b + 1\n"
FIXED = "def add(a, b):\n    return a + b\n"
F2P = "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _patch_text(before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile="a/calc.py",
            tofile="b/calc.py",
            lineterm="\n",
        )
    )


def _make_base(tmp_path: Path) -> Path:
    fixture = tmp_path / "fixtures" / "calc"
    _write(fixture / "calc.py", BUGGY)
    _write(fixture / "tests" / "test_f2p.py", F2P)
    _write(tmp_path / "patches" / "fix.patch", _patch_text(BUGGY, FIXED))
    return tmp_path


def _v2_task() -> EvalTaskV2:
    return EvalTaskV2.model_validate(
        {
            "schema_version": 2,
            "id": "evidence-demo",
            "category": "edit",
            "fixture": "fixtures/calc",
            "message": "Fix add().",
            "oracle": {"f2p": [PY + ["-m", "pytest", "-q", "tests/test_f2p.py"]]},
            "integrity": {"max_files_changed": 2, "max_lines_changed": 20},
            "gold_patch": "patches/fix.patch",
        }
    )


def _fix_provider(_task: object) -> FakeProvider:
    return FakeProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        call_id="c0",
                        name="write_file",
                        arguments={"path": "calc.py", "content": FIXED},
                    )
                ]
            ),
            ModelResponse(content="done"),
        ]
    )


def test_v2_task_evidence_layout(tmp_path: Path) -> None:
    base = _make_base(tmp_path)
    artifacts = tmp_path / "artifacts"

    report = asyncio.run(
        run_tasks(
            [_v2_task()],
            base,
            provider_factory=_fix_provider,
            evidence_root=artifacts,
        )
    )

    assert report.passed == 1
    layout = EvidenceLayout(artifacts)
    task_dir = layout.task_dir("evidence-demo")
    assert (task_dir / "outcome.json").is_file()
    assert (task_dir / "integrity.json").is_file()
    assert (task_dir / "f2p_0.log").is_file()
    assert (task_dir / "f2p_0.xml").is_file()

    outcome = json.loads((task_dir / "outcome.json").read_text(encoding="utf-8"))
    assert outcome["status"] == "passed"

    diff = (task_dir / "candidate.diff").read_text(encoding="utf-8")
    assert "diff --git a/calc.py b/calc.py" in diff
    assert "+    return a + b\n" in diff
    assert "-    return a + b + 1" in diff


def test_v1_task_writes_only_outcome(tmp_path: Path) -> None:
    base = _make_base(tmp_path)
    artifacts = tmp_path / "artifacts"
    task = EvalTask.model_validate(
        {
            "id": "v1-demo",
            "fixture": "fixtures/calc",
            "message": "Fix add().",
            "checks": [
                {"type": "file_contains", "path": "calc.py", "value": "return a + b"}
            ],
        }
    )

    report = asyncio.run(
        run_tasks(
            [task],
            base,
            provider_factory=_fix_provider,
            evidence_root=artifacts,
        )
    )

    assert report.passed == 1
    files = sorted(
        path.name for path in (artifacts / "tasks" / "v1-demo" / "0").iterdir()
    )
    assert files == ["outcome.json"]


def test_manifest_and_report_artifacts_roundtrip(tmp_path: Path) -> None:
    from bale_harness.eval.evidence import write_manifest, write_report

    layout = EvidenceLayout(tmp_path / "artifacts")
    layout.ensure()
    manifest = build_manifest(
        [_v2_task()],
        _make_base(tmp_path),
        Settings(),
        run_id="fixed-run",
    )
    report = EvalReport(
        started_at="2026-01-01T00:00:00+00:00",
        duration_ms=0,
        total=0,
        passed=0,
        failed=0,
        errors=0,
    )
    report.manifest = manifest

    write_manifest(layout, manifest)
    write_report(layout, report, "# report\n")

    stored = json.loads(layout.manifest_path.read_text(encoding="utf-8"))
    assert stored["run_id"] == "fixed-run"
    assert (
        json.loads(layout.report_json_path.read_text(encoding="utf-8"))["manifest"][
            "run_id"
        ]
        == "fixed-run"
    )
    assert layout.report_md_path.read_text(encoding="utf-8") == "# report\n"


def test_render_diff_handles_add_modify_delete_and_binary(tmp_path: Path) -> None:
    pristine = tmp_path / "pristine"
    candidate = tmp_path / "candidate"
    _write(pristine / "mod.py", "a = 1\n")
    _write(candidate / "mod.py", "a = 2\n")
    _write(candidate / "new.py", "b = 1\n")
    _write(pristine / "gone.py", "c = 1\n")
    (pristine / "blob.bin").write_bytes(b"\x00\x01")
    (candidate / "blob.bin").write_bytes(b"\x00\x02")

    diff = render_diff(pristine, candidate)

    assert "diff --git a/mod.py b/mod.py" in diff
    assert "-a = 1" in diff and "+a = 2" in diff
    assert "--- /dev/null" in diff and "+++ b/new.py" in diff
    assert "--- a/gone.py" in diff and "+++ /dev/null" in diff
    assert "Binary files a/blob.bin and b/blob.bin differ" in diff
