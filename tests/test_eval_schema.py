"""Tests for the versioned task schema and suite loader (E1 commit 1)."""

import json
from pathlib import Path

import pytest

from bale_harness.eval import (
    EvalTaskError,
    EvalTaskV2,
    load_suite,
    load_tasks,
)


def _write(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _v2_task(**overrides: object) -> dict[str, object]:
    task: dict[str, object] = {
        "id": "t1",
        "category": "edit",
        "fixture": "fixtures/demo",
        "message": "fix it",
        "oracle": {"file_checks": [{"type": "file_exists", "path": "a.py"}]},
    }
    task.update(overrides)
    return task


def test_v2_task_with_file_checks_loads(tmp_path: Path) -> None:
    path = _write(tmp_path / "tasks.json", {"schema_version": 2, "tasks": [_v2_task()]})

    tasks = load_tasks(path)

    assert len(tasks) == 1
    assert isinstance(tasks[0], EvalTaskV2)
    assert tasks[0].category == "edit"


def test_v2_empty_oracle_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "tasks.json",
        {"schema_version": 2, "tasks": [_v2_task(oracle={})]},
    )

    with pytest.raises(EvalTaskError):
        load_tasks(path)


def test_v2_commands_require_gold_patch(tmp_path: Path) -> None:
    missing = _v2_task(oracle={"f2p": [["python", "-m", "pytest", "tests/test_x.py"]]})
    with pytest.raises(EvalTaskError):
        load_tasks(
            _write(tmp_path / "missing.json", {"schema_version": 2, "tasks": [missing]})
        )

    valid = _v2_task(
        oracle={"f2p": [["python", "-m", "pytest"]]},
        gold_patch="patches/fix.patch",
    )
    tasks = load_tasks(
        _write(tmp_path / "valid.json", {"schema_version": 2, "tasks": [valid]})
    )
    assert tasks[0].gold_patch == "patches/fix.patch"


def test_v2_empty_argv_is_rejected(tmp_path: Path) -> None:
    task = _v2_task(oracle={"f2p": [[]]}, gold_patch="p.patch")
    path = _write(tmp_path / "tasks.json", {"schema_version": 2, "tasks": [task]})

    with pytest.raises(EvalTaskError):
        load_tasks(path)


@pytest.mark.parametrize("bad", ["../escape", "/abs/path", "a/../../b"])
def test_v2_fixture_escape_is_rejected(tmp_path: Path, bad: str) -> None:
    path = _write(
        tmp_path / f"task_{abs(hash(bad))}.json",
        {"schema_version": 2, "tasks": [_v2_task(fixture=bad)]},
    )

    with pytest.raises(EvalTaskError):
        load_tasks(path)


def test_v2_duplicate_ids_are_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "tasks.json",
        {
            "schema_version": 2,
            "tasks": [_v2_task(id="same"), _v2_task(id="same", message="two")],
        },
    )

    with pytest.raises(EvalTaskError):
        load_tasks(path)


def test_v1_file_still_loads(tmp_path: Path) -> None:
    task = {
        "id": "t1",
        "fixture": "fixtures/greeting",
        "message": "do it",
        "checks": [{"type": "file_exists", "path": "a.py"}],
    }
    path = _write(tmp_path / "tasks.json", [task])

    tasks = load_tasks(path)

    assert tasks[0].id == "t1"
    assert not isinstance(tasks[0], EvalTaskV2)


def test_load_suite_parses_manifest(tmp_path: Path) -> None:
    manifest = {
        "schema_version": 2,
        "suite": "golden",
        "suite_version": "1.0.0",
        "tasks": [_v2_task(id="t1")],
    }
    path = _write(tmp_path / "suite.json", manifest)

    loaded_manifest, tasks = load_suite(path)

    assert loaded_manifest.suite == "golden"
    assert loaded_manifest.suite_version == "1.0.0"
    assert tasks[0].id == "t1"


def test_load_suite_rejects_missing_metadata(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "suite.json",
        {"schema_version": 2, "tasks": [_v2_task()]},
    )

    with pytest.raises(EvalTaskError):
        load_suite(path)


def test_v2_budget_nested_parses(tmp_path: Path) -> None:
    task = _v2_task(
        budget={"max_iterations": 12, "token_budget": 32768, "time_budget_s": 180}
    )
    path = _write(tmp_path / "tasks.json", {"schema_version": 2, "tasks": [task]})

    loaded = load_tasks(path)

    assert loaded[0].budget.max_iterations == 12
    assert loaded[0].budget.token_budget == 32768
    assert loaded[0].budget.time_budget_s == 180


def test_v2_budget_defaults_match_e1(tmp_path: Path) -> None:
    path = _write(tmp_path / "tasks.json", {"schema_version": 2, "tasks": [_v2_task()]})

    loaded = load_tasks(path)

    assert loaded[0].budget.max_iterations == 8
    assert loaded[0].budget.token_budget == 128_000
    assert loaded[0].budget.time_budget_s is None


def test_v2_flat_budget_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "tasks.json",
        {"schema_version": 2, "tasks": [_v2_task(max_iterations=12)]},
    )

    with pytest.raises(EvalTaskError):
        load_tasks(path)


def test_v2_forbidden_side_effects_accepts_known_values(tmp_path: Path) -> None:
    task = _v2_task(
        integrity={
            "forbidden_side_effects": ["network", "git_push", "workspace_escape"]
        }
    )
    path = _write(tmp_path / "tasks.json", {"schema_version": 2, "tasks": [task]})

    loaded = load_tasks(path)

    assert loaded[0].integrity.forbidden_side_effects == [
        "network",
        "git_push",
        "workspace_escape",
    ]


def test_v2_forbidden_side_effects_rejects_unknown(tmp_path: Path) -> None:
    task = _v2_task(integrity={"forbidden_side_effects": ["foo"]})
    path = _write(tmp_path / "tasks.json", {"schema_version": 2, "tasks": [task]})

    with pytest.raises(EvalTaskError):
        load_tasks(path)


def test_v2_tool_contracts_parse(tmp_path: Path) -> None:
    task = _v2_task(
        required_tools=["write_file"],
        forbidden_tools=["http_request"],
    )
    path = _write(tmp_path / "tasks.json", {"schema_version": 2, "tasks": [task]})

    loaded = load_tasks(path)

    assert loaded[0].required_tools == ["write_file"]
    assert loaded[0].forbidden_tools == ["http_request"]
