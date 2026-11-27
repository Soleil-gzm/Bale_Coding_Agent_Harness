"""Golden suite structure and invariant checks (E2).

For now the suite only contains the ``read`` and ``security`` fixtures. The
full 5/6/3/3/3 category gate and per-task preflight checks are added together
with the edit/test/recovery fixtures.
"""

from pathlib import Path

from bale_harness.eval import load_suite, validate_suite_categories

GOLDEN_ROOT = Path(__file__).resolve().parents[1] / "examples" / "eval" / "golden"
SUITE_PATH = GOLDEN_ROOT / "suite.json"


def _load():
    return load_suite(SUITE_PATH)


def test_suite_loads_read_and_security_tasks() -> None:
    manifest, tasks = _load()

    assert manifest.suite == "golden"
    assert len(tasks) == 8
    assert {task.category for task in tasks} == {"read", "security"}


def test_present_category_counts() -> None:
    _manifest, tasks = _load()

    ok, actual = validate_suite_categories(tasks)

    # Only read/security exist so far; the full gate lands with the other tasks.
    assert ok is False
    assert actual["read"] == 5
    assert actual["security"] == 3
    assert actual["edit"] == 0


def test_read_tasks_are_single_read_only_checks() -> None:
    _manifest, tasks = _load()

    for task in tasks:
        if task.category != "read":
            continue
        assert task.scenario.type == "single"
        assert task.integrity.read_only is True
        assert task.gold_patch is None
        assert task.oracle.has_commands is False
        assert (GOLDEN_ROOT / task.fixture).is_dir()


def test_security_tasks_use_security_scenario() -> None:
    _manifest, tasks = _load()

    for task in tasks:
        if task.category != "security":
            continue
        assert task.scenario.type == "security"
        assert task.gold_patch is None
        assert (GOLDEN_ROOT / task.fixture).is_dir()


def test_security_tasks_declare_forbidden_side_effects() -> None:
    _manifest, tasks = _load()
    by_id = {task.id: task for task in tasks}

    assert by_id["security-path-escape"].integrity.forbidden_side_effects == [
        "workspace_escape"
    ]
    assert by_id["security-network-gitpush"].integrity.forbidden_side_effects == [
        "network",
        "git_push",
    ]
