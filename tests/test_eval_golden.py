"""Golden suite structure and invariant checks (E2).

The suite currently contains the ``read``, ``edit``, and ``security``
fixtures. The full 5/6/3/3/3 category gate is added together with the
test/recovery fixtures.
"""

from pathlib import Path

from bale_harness.eval import load_suite, preflight_task, validate_suite_categories

GOLDEN_ROOT = Path(__file__).resolve().parents[1] / "examples" / "eval" / "golden"
SUITE_PATH = GOLDEN_ROOT / "suite.json"


def _load():
    return load_suite(SUITE_PATH)


def test_suite_loads_present_tasks() -> None:
    manifest, tasks = _load()

    assert manifest.suite == "golden"
    assert len(tasks) == 14
    assert {task.category for task in tasks} == {"read", "edit", "security"}


def test_present_category_counts() -> None:
    _manifest, tasks = _load()

    ok, actual = validate_suite_categories(tasks)

    # test/recovery are still missing; the full gate lands with those tasks.
    assert ok is False
    assert actual["read"] == 5
    assert actual["edit"] == 6
    assert actual["security"] == 3
    assert actual["test"] == 0


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


def test_edit_tasks_declare_commands_and_gold_patch() -> None:
    _manifest, tasks = _load()

    for task in tasks:
        if task.category != "edit":
            continue
        assert task.scenario.type == "single"
        assert task.integrity.read_only is False
        assert task.oracle.f2p and task.oracle.p2p
        assert task.gold_patch is not None
        assert (GOLDEN_ROOT / task.gold_patch).is_file()
        assert (GOLDEN_ROOT / task.fixture).is_dir()


def test_edit_tasks_pass_preflight() -> None:
    _manifest, tasks = _load()
    edit_tasks = [task for task in tasks if task.category == "edit"]

    assert len(edit_tasks) == 6
    for task in edit_tasks:
        result = preflight_task(task, GOLDEN_ROOT)
        assert result.ok is True, f"{task.id}: {result.error}"
        assert result.pristine_f2p_failed is True
        assert result.pristine_p2p_passed is True
        assert result.gold_all_passed is True
