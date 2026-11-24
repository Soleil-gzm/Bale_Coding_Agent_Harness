import json
from pathlib import Path

from typer.testing import CliRunner

from bale_harness.cli import app

runner = CliRunner()


def test_run_completes_end_to_end_with_fake_provider(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("BALE_PROVIDER", "fake")
    (tmp_path / "README.md").write_text("# demo", encoding="utf-8")

    result = runner.invoke(
        app,
        ["run", "--message", "inspect the workspace", "--workspace", str(tmp_path)],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["status"] == "completed"
    assert data["answer"] == "Task completed."
    assert data["iterations"] >= 1
    assert data["tool_calls"] == 0


def test_run_rejects_missing_workspace(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BALE_PROVIDER", "fake")
    missing = tmp_path / "does_not_exist"

    result = runner.invoke(app, ["run", "--message", "x", "--workspace", str(missing)])

    assert result.exit_code != 0
