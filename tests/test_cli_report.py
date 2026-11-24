import asyncio
from pathlib import Path
from uuid import uuid4

from typer.testing import CliRunner

from bale_harness.agent.session import SessionStore
from bale_harness.cli import app
from bale_harness.contracts import Turn

runner = CliRunner()


def _seed(db: Path, workspace: Path, session_id) -> None:
    async def scenario() -> None:
        store = SessionStore(db)
        turn = Turn(
            message="write docs",
            workspace=workspace,
            session_id=session_id,
            token_budget=1024,
            max_iterations=3,
        )
        await store.start_turn(turn)
        await store.finish_turn(turn.turn_id, "completed", answer="done")

    asyncio.run(scenario())


def test_report_prints_markdown(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    session_id = uuid4()
    _seed(db, tmp_path, session_id)

    result = runner.invoke(
        app,
        ["report", "--session-db", str(db), "--session-id", str(session_id)],
    )

    assert result.exit_code == 0, result.output
    assert "# Bale Run Report" in result.output
    assert "write docs" in result.output
    assert str(session_id) in result.output


def test_report_writes_output_file(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    session_id = uuid4()
    _seed(db, tmp_path, session_id)
    output = tmp_path / "reports" / "run.md"

    result = runner.invoke(
        app,
        [
            "report",
            "--session-db",
            str(db),
            "--session-id",
            str(session_id),
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "wrote" in result.output
    assert "# Bale Run Report" in output.read_text(encoding="utf-8")


def test_report_rejects_unknown_session(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    _seed(db, tmp_path, uuid4())

    result = runner.invoke(
        app,
        ["report", "--session-db", str(db), "--session-id", str(uuid4())],
    )

    assert result.exit_code != 0


def test_report_rejects_invalid_session_id(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    _seed(db, tmp_path, uuid4())

    result = runner.invoke(
        app,
        ["report", "--session-db", str(db), "--session-id", "not-a-uuid"],
    )

    assert result.exit_code != 0


def test_report_rejects_missing_trace(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    session_id = uuid4()
    _seed(db, tmp_path, session_id)

    result = runner.invoke(
        app,
        [
            "report",
            "--session-db",
            str(db),
            "--session-id",
            str(session_id),
            "--trace",
            str(tmp_path / "missing.jsonl"),
        ],
    )

    assert result.exit_code != 0
