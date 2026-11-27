"""Tests for the v2 three-workspace runner, report rendering, and preflight."""

import asyncio
import difflib
import sys
from pathlib import Path

from bale_harness.agent.messages import ModelResponse, ToolCall
from bale_harness.eval import (
    EvalTask,
    EvalTaskV2,
    fixture_directory,
    preflight_task,
    render_markdown,
    run_task,
    run_tasks,
)
from bale_harness.providers import FakeProvider

PY = [sys.executable, "-m", "pytest", "-q"]
BUGGY = "def total(price, qty):\n    return price * qty\n"
FIXED = "def total(price, qty, discount=0):\n    return price * qty - discount\n"
BUGGY_COMMENT = "# placeholder\n" + BUGGY
F2P_NODE = "tests/test_pricing.py::test_f2p_discount"
P2P_NODE = "tests/test_pricing.py::test_p2p_basic"
TESTS = (
    "import pricing\n\n"
    "def test_f2p_discount():\n"
    "    assert pricing.total(100, 2, discount=10) == 190\n\n"
    "def test_p2p_basic():\n"
    "    assert pricing.total(10, 3) == 30\n"
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _patch_text(before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile="pricing.py",
            tofile="pricing.py",
            lineterm="\n",
        )
    )


def _make_fixture(base: Path, pricing: str = BUGGY, patch: str | None = None) -> Path:
    fixture = base / "fixtures" / "pricing"
    _write(fixture / "pricing.py", pricing)
    _write(fixture / "tests" / "test_pricing.py", TESTS)
    _write(
        base / "patches" / "fix.patch",
        patch if patch is not None else _patch_text(BUGGY, FIXED),
    )
    return base


def _v2_task(**overrides: object) -> EvalTaskV2:
    payload: dict[str, object] = {
        "schema_version": 2,
        "id": "discount",
        "category": "edit",
        "fixture": "fixtures/pricing",
        "message": "Add discount support to total().",
        "oracle": {
            "f2p": [PY + [F2P_NODE]],
            "p2p": [PY + [P2P_NODE]],
            "hidden": [],
            "file_checks": [],
        },
        "integrity": {
            "forbidden_changes": ["tests/**"],
            "max_files_changed": 4,
            "max_lines_changed": 200,
        },
        "gold_patch": "patches/fix.patch",
    }
    payload.update(overrides)
    return EvalTaskV2.model_validate(payload)


def _provider_with_writes(*writes: tuple[str, str]) -> FakeProvider:
    calls = [
        ToolCall(
            call_id=f"c{i}", name="write_file", arguments={"path": p, "content": c}
        )
        for i, (p, c) in enumerate(writes)
    ]
    return FakeProvider(
        [ModelResponse(tool_calls=calls), ModelResponse(content="done")]
    )


def _fix_provider(_task: EvalTaskV2) -> FakeProvider:
    return _provider_with_writes(("pricing.py", FIXED))


def _tampers_test_provider(_task: EvalTaskV2) -> FakeProvider:
    bad = (
        "import pytest\n\n"
        "@pytest.mark.skip\n"
        "def test_f2p_discount():\n"
        "    assert pricing.total(100, 2, discount=10) == 190\n\n"
        "def test_p2p_basic():\n"
        "    assert pricing.total(10, 3) == 30\n"
    )
    return _provider_with_writes(("tests/test_pricing.py", bad))


def _extra_files_provider(_task: EvalTaskV2) -> FakeProvider:
    return _provider_with_writes(("a.py", "a = 1\n"), ("b.py", "b = 2\n"))


def _v1_writing_provider(_task: EvalTask) -> FakeProvider:
    return _provider_with_writes(("greetings.py", 'def greet():\n    return "hello"\n'))


def _v1_greeting_fixture(base: Path) -> Path:
    fixture = base / "fixtures" / "greeting"
    _write(fixture / "greetings.py", 'def greet():\n    return "hi"\n')
    return base


def _v1_task() -> EvalTask:
    return EvalTask.model_validate(
        {
            "id": "greeting",
            "fixture": "fixtures/greeting",
            "message": "Change the greeting to hello.",
            "checks": [
                {"type": "file_contains", "path": "greetings.py", "value": "hello"}
            ],
        }
    )


def test_v2_resolved_when_agent_fixes_bug(tmp_path: Path) -> None:
    base = _make_fixture(tmp_path)
    task = _v2_task()

    outcome = asyncio.run(
        run_task(task, fixture_directory(task, base), _fix_provider(task))
    )

    assert outcome.status == "passed"
    assert outcome.integrity is not None and outcome.integrity.ok
    assert outcome.oracle is not None and outcome.oracle.all_passed
    assert outcome.oracle.f2p.status == "passed"
    assert outcome.oracle.p2p.status == "passed"


def test_v2_fails_when_agent_tampers_protected_test(tmp_path: Path) -> None:
    base = _make_fixture(tmp_path)
    task = _v2_task()

    outcome = asyncio.run(
        run_task(task, fixture_directory(task, base), _tampers_test_provider(task))
    )

    assert outcome.status == "failed"
    assert outcome.integrity is not None and not outcome.integrity.ok
    assert any(v.code == "forbidden_change" for v in outcome.integrity.violations)
    assert "integrity" in (outcome.error or "")


def test_v2_fails_when_exceeds_max_files_changed(tmp_path: Path) -> None:
    base = _make_fixture(tmp_path)
    task = _v2_task(integrity={"forbidden_changes": [], "max_files_changed": 1})

    outcome = asyncio.run(
        run_task(task, fixture_directory(task, base), _extra_files_provider(task))
    )

    assert outcome.status == "failed"
    assert outcome.integrity is not None
    assert any(v.code == "max_files" for v in outcome.integrity.violations)


def test_v1_greeting_still_passes_without_v2_fields(tmp_path: Path) -> None:
    base = _v1_greeting_fixture(tmp_path)
    task = _v1_task()

    outcome = asyncio.run(
        run_task(task, fixture_directory(task, base), _v1_writing_provider(task))
    )

    assert outcome.status == "passed"
    assert outcome.run_status == "completed"
    assert outcome.check_results[0].passed is True
    assert outcome.oracle is None
    assert outcome.integrity is None


def test_render_markdown_v1_excludes_oracle_and_integrity(tmp_path: Path) -> None:
    base = _v1_greeting_fixture(tmp_path)
    report = asyncio.run(
        run_tasks([_v1_task()], base, provider_factory=_v1_writing_provider)
    )

    markdown = render_markdown(report)

    assert "Integrity:" not in markdown
    assert "Oracle:" not in markdown


def test_render_markdown_v2_includes_oracle_and_integrity(tmp_path: Path) -> None:
    base = _make_fixture(tmp_path)
    report = asyncio.run(run_tasks([_v2_task()], base, provider_factory=_fix_provider))

    markdown = render_markdown(report)

    assert report.passed == 1
    assert "Integrity: ok" in markdown
    assert "Oracle: passed" in markdown


def test_preflight_passes_for_valid_task(tmp_path: Path) -> None:
    base = _make_fixture(tmp_path)

    result = preflight_task(_v2_task(), base)

    assert result.ok is True
    assert result.pristine_f2p_failed is True
    assert result.pristine_p2p_passed is True
    assert result.gold_all_passed is True


def test_preflight_fails_when_gold_patch_missing(tmp_path: Path) -> None:
    base = _make_fixture(tmp_path)
    task = _v2_task(gold_patch="patches/missing.patch")

    result = preflight_task(task, base)

    assert result.ok is False
    assert "gold_patch not found" in (result.error or "")


def test_preflight_fails_when_gold_does_not_fix_f2p(tmp_path: Path) -> None:
    base = _make_fixture(tmp_path, patch=_patch_text(BUGGY, BUGGY_COMMENT))

    result = preflight_task(_v2_task(), base)

    assert result.ok is False
    assert result.pristine_f2p_failed is True
    assert result.gold_all_passed is False
    assert "gold patch does not pass" in (result.error or "")


def test_preflight_fails_when_f2p_already_passes(tmp_path: Path) -> None:
    base = _make_fixture(
        tmp_path, pricing=FIXED, patch=_patch_text(FIXED, "# note\n" + FIXED)
    )

    result = preflight_task(_v2_task(), base)

    assert result.ok is False
    assert result.pristine_f2p_failed is False
    assert "f2p does not fail on pristine" in (result.error or "")


HIDDEN_TESTS = (
    "import pricing\n\n"
    "def test_hidden_discount_large():\n"
    "    assert pricing.total(200, 3, discount=50) == 550\n"
)


def _make_fixture_with_hidden(base: Path) -> Path:
    base = _make_fixture(base)
    _write(base / "verifier" / "discount" / "hidden" / "test_hidden.py", HIDDEN_TESTS)
    return base


def _v2_task_with_hidden() -> EvalTaskV2:
    return _v2_task(
        oracle={
            "f2p": [PY + [F2P_NODE]],
            "p2p": [PY + [P2P_NODE]],
            "hidden": [PY + ["hidden/test_hidden.py"]],
            "file_checks": [],
        }
    )


def test_v2_hidden_tests_are_injected_and_pass(tmp_path: Path) -> None:
    base = _make_fixture_with_hidden(tmp_path)
    task = _v2_task_with_hidden()

    report = asyncio.run(run_tasks([task], base, provider_factory=_fix_provider))

    assert report.passed == 1
    outcome = report.outcomes[0]
    assert outcome.oracle is not None
    assert outcome.oracle.hidden.status == "passed"
    assert outcome.oracle.all_passed


def test_preflight_passes_when_hidden_tests_present(tmp_path: Path) -> None:
    base = _make_fixture_with_hidden(tmp_path)

    result = preflight_task(_v2_task_with_hidden(), base)

    assert result.ok is True
    assert result.gold_all_passed is True
