import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from bale_harness.agent.messages import ModelResponse, ToolCall
from bale_harness.cli import app
from bale_harness.eval import (
    Check,
    EvalTask,
    EvalTaskError,
    evaluate_checks,
    fixture_directory,
    load_tasks,
    render_markdown,
    run_tasks,
)
from bale_harness.providers import FakeProvider

runner = CliRunner()


def _write_tasks(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _fixture(tmp_path: Path) -> Path:
    fixture = tmp_path / "fixtures" / "greeting"
    fixture.mkdir(parents=True)
    (fixture / "greetings.py").write_text(
        'def greet():\n    return "hi"\n', encoding="utf-8"
    )
    return fixture


def _task(**overrides: object) -> EvalTask:
    payload: dict[str, object] = {
        "id": "greeting",
        "fixture": "fixtures/greeting",
        "message": "Change the greeting to hello.",
        "checks": [{"type": "file_contains", "path": "greetings.py", "value": "hello"}],
    }
    payload.update(overrides)
    return EvalTask.model_validate(payload)


def _writing_provider(_task: EvalTask) -> FakeProvider:
    return FakeProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        call_id="c1",
                        name="write_file",
                        arguments={
                            "path": "greetings.py",
                            "content": 'def greet():\n    return "hello"\n',
                        },
                    )
                ]
            ),
            ModelResponse(content="done"),
        ]
    )


def test_load_tasks_accepts_list_and_document(tmp_path: Path) -> None:
    task = {
        "id": "t1",
        "fixture": "fixtures/greeting",
        "message": "do it",
    }
    list_file = _write_tasks(tmp_path, [task])
    document_file = tmp_path / "document.json"
    document_file.write_text(json.dumps({"tasks": [task]}), encoding="utf-8")

    assert [item.id for item in load_tasks(list_file)] == ["t1"]
    assert [item.id for item in load_tasks(document_file)] == ["t1"]


def test_load_tasks_resolves_fixture_directory(tmp_path: Path) -> None:
    tasks_file = _write_tasks(
        tmp_path,
        [{"id": "t1", "fixture": "fixtures/greeting", "message": "do it"}],
    )

    task = load_tasks(tasks_file)[0]

    assert (
        fixture_directory(task, tasks_file.parent)
        == (tmp_path / "fixtures" / "greeting").resolve()
    )


def test_load_tasks_rejects_invalid_checks(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        Check(type="file_contains", path="a.py")
    with pytest.raises(ValidationError):
        Check(type="file_exists", path="a.py", value="unexpected")

    bad_file = _write_tasks(
        tmp_path,
        [
            {
                "id": "t1",
                "fixture": "fixtures/greeting",
                "message": "do it",
                "checks": [{"type": "file_contains", "path": "a.py"}],
            }
        ],
    )
    with pytest.raises(EvalTaskError):
        load_tasks(bad_file)


def test_load_tasks_rejects_duplicates_and_bad_json(tmp_path: Path) -> None:
    duplicate = _write_tasks(
        tmp_path,
        [
            {"id": "same", "fixture": "a", "message": "one"},
            {"id": "same", "fixture": "b", "message": "two"},
        ],
    )
    with pytest.raises(EvalTaskError):
        load_tasks(duplicate)

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(EvalTaskError):
        load_tasks(broken)
    with pytest.raises(EvalTaskError):
        load_tasks(tmp_path / "missing.json")


def test_evaluate_checks_covers_all_check_types(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("hello world\n", encoding="utf-8")
    checks = [
        Check(type="file_exists", path="a.py"),
        Check(type="file_not_exists", path="b.py"),
        Check(type="file_contains", path="a.py", value="hello"),
        Check(type="file_not_contains", path="a.py", value="goodbye"),
        Check(type="file_contains", path="a.py", value="missing"),
        Check(type="file_exists", path="../escape.py"),
    ]

    results = evaluate_checks(tmp_path, checks)

    assert [result.passed for result in results] == [
        True,
        True,
        True,
        True,
        False,
        False,
    ]
    assert results[-1].detail == "path escapes workspace"


def test_run_tasks_passes_with_scripted_provider(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)

    report = asyncio.run(
        run_tasks([_task()], tmp_path, provider_factory=_writing_provider)
    )

    assert report.total == 1
    assert report.passed == 1
    assert report.ok is True
    outcome = report.outcomes[0]
    assert outcome.status == "passed"
    assert outcome.run_status == "completed"
    assert outcome.check_results[0].passed is True
    assert (fixture / "greetings.py").read_text(encoding="utf-8") == (
        'def greet():\n    return "hi"\n'
    )


def test_run_tasks_fails_when_checks_fail(tmp_path: Path) -> None:
    _fixture(tmp_path)

    report = asyncio.run(run_tasks([_task()], tmp_path))

    assert report.failed == 1
    assert report.ok is False
    outcome = report.outcomes[0]
    assert outcome.status == "failed"
    assert outcome.run_status == "completed"
    assert "check" in (outcome.error or "")
    assert outcome.check_results[0].passed is False


def test_run_tasks_reports_missing_fixture(tmp_path: Path) -> None:
    task = _task(fixture="fixtures/missing")

    report = asyncio.run(run_tasks([task], tmp_path))

    assert report.errors == 1
    assert report.outcomes[0].status == "error"
    assert "fixture not found" in (report.outcomes[0].error or "")


def test_render_markdown_includes_summary_and_details(tmp_path: Path) -> None:
    _fixture(tmp_path)
    report = asyncio.run(
        run_tasks([_task()], tmp_path, provider_factory=_writing_provider)
    )

    markdown = render_markdown(report)

    assert "# Evaluation Report" in markdown
    assert "| Task | Status | Run | Iterations | Tools | Tokens | Checks |" in markdown
    assert "| `greeting` | passed | completed |" in markdown
    assert "PASS `file_contains` `greetings.py`" in markdown


def test_cli_eval_writes_reports_and_fails_on_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BALE_PROVIDER", "fake")
    _fixture(tmp_path)
    tasks_file = _write_tasks(
        tmp_path,
        [
            {
                "id": "greeting",
                "fixture": "fixtures/greeting",
                "message": "Change the greeting to hello.",
                "checks": [
                    {
                        "type": "file_contains",
                        "path": "greetings.py",
                        "value": "hello",
                    }
                ],
            }
        ],
    )
    markdown_path = tmp_path / "report.md"
    json_path = tmp_path / "report.json"

    result = runner.invoke(
        app,
        [
            "eval",
            "--tasks",
            str(tasks_file),
            "--output",
            str(markdown_path),
            "--json",
            str(json_path),
        ],
    )

    assert result.exit_code == 1, result.output
    assert "passed 0/1" in result.output
    assert markdown_path.exists()
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["total"] == 1
    assert payload["failed"] == 1
    assert payload["outcomes"][0]["task_id"] == "greeting"


def test_cli_eval_rejects_invalid_task_file(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")

    result = runner.invoke(app, ["eval", "--tasks", str(broken)])

    assert result.exit_code != 0
