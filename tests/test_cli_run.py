import asyncio
import json
from pathlib import Path
from uuid import UUID, uuid4

from typer.testing import CliRunner

from bale_harness.agent.session import SessionStore
from bale_harness.cli import app
from bale_harness.contracts import Turn

runner = CliRunner()


def _prepare_cancelled_turn(db: Path, workspace: Path, session_id: UUID) -> None:
    async def scenario() -> None:
        store = SessionStore(db)
        turn = Turn(
            message="pending work",
            workspace=workspace,
            session_id=session_id,
            token_budget=4096,
            max_iterations=2,
        )
        await store.start_turn(turn)
        await store.finish_turn(turn.turn_id, "cancelled", error="cancelled")

    asyncio.run(scenario())


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


def test_run_persists_session_when_db_is_given(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BALE_PROVIDER", "fake")
    db = tmp_path / "sessions.db"
    session_id = uuid4()

    result = runner.invoke(
        app,
        [
            "run",
            "--message",
            "inspect",
            "--workspace",
            str(tmp_path),
            "--session-db",
            str(db),
            "--session-id",
            str(session_id),
            "--no-shadow-git",
        ],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["status"] == "completed"
    assert data["session_id"] == str(session_id)
    assert db.exists()


def test_resume_continues_without_a_new_message(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BALE_PROVIDER", "fake")
    db = tmp_path / "sessions.db"
    session_id = uuid4()
    _prepare_cancelled_turn(db, tmp_path, session_id)

    result = runner.invoke(
        app,
        [
            "run",
            "--workspace",
            str(tmp_path),
            "--session-db",
            str(db),
            "--session-id",
            str(session_id),
            "--resume",
            "--no-shadow-git",
        ],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["status"] == "completed"
    assert data["resumed"] is True

    async def messages():
        return await SessionStore(db).load_messages(session_id)

    stored = asyncio.run(messages())
    assert [message.content for message in stored if message.role == "user"] == [
        "pending work"
    ]


def test_resume_rejects_message_flag(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BALE_PROVIDER", "fake")
    db = tmp_path / "sessions.db"
    session_id = uuid4()

    result = runner.invoke(
        app,
        [
            "run",
            "--message",
            "extra",
            "--workspace",
            str(tmp_path),
            "--session-db",
            str(db),
            "--session-id",
            str(session_id),
            "--resume",
        ],
    )

    assert result.exit_code != 0


def test_run_writes_trace_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BALE_PROVIDER", "fake")
    trace = tmp_path / "traces" / "run.jsonl"

    result = runner.invoke(
        app,
        [
            "run",
            "--message",
            "inspect",
            "--workspace",
            str(tmp_path),
            "--trace",
            str(trace),
            "--no-shadow-git",
        ],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["status"] == "completed"
    assert data["duration_ms"] >= 0
    assert data["estimated_cost"] is None

    events = [
        json.loads(line)
        for line in trace.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert events[0]["event"] == "turn_started"
    assert events[-1]["event"] == "turn_finished"
    assert events[-1]["status"] == "completed"
