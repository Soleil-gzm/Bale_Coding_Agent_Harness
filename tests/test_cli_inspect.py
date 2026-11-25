import asyncio
import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from typer.testing import CliRunner

from bale_harness.agent.checkpoint import ShadowGit
from bale_harness.agent.session import SessionStore
from bale_harness.agent.trace import JsonlTraceExporter
from bale_harness.cli import app
from bale_harness.contracts import Turn

runner = CliRunner()


def _seed(db: Path, workspace: Path, session_id: UUID, message: str = "write docs"):
    async def scenario() -> None:
        store = SessionStore(db)
        turn = Turn(
            message=message,
            workspace=workspace,
            session_id=session_id,
            token_budget=1024,
            max_iterations=3,
        )
        await store.start_turn(turn)
        await store.finish_turn(turn.turn_id, "completed", answer="done")

    asyncio.run(scenario())


def test_session_list_prints_summary(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    session_id = uuid4()
    _seed(db, tmp_path, session_id)

    result = runner.invoke(app, ["session", "list", "--session-db", str(db)])

    assert result.exit_code == 0, result.output
    assert str(session_id) in result.output
    assert "turns=1" in result.output


def test_session_list_json_is_machine_readable(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    session_id = uuid4()
    _seed(db, tmp_path, session_id)

    result = runner.invoke(app, ["session", "list", "--session-db", str(db), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload[0]["session_id"] == str(session_id)
    assert payload[0]["turns"] == 1


def test_session_show_prints_turns(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    session_id = uuid4()
    _seed(db, tmp_path, session_id)

    result = runner.invoke(
        app,
        ["session", "show", "--session-db", str(db), "--session-id", str(session_id)],
    )

    assert result.exit_code == 0, result.output
    assert "write docs" in result.output
    assert "completed" in result.output


def test_session_show_rejects_unknown_session(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    _seed(db, tmp_path, uuid4())

    result = runner.invoke(
        app,
        ["session", "show", "--session-db", str(db), "--session-id", str(uuid4())],
    )

    assert result.exit_code != 0


def test_trace_summarizes_events(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    turn = Turn(message="hi", workspace=tmp_path, token_budget=1024, max_iterations=2)

    async def scenario() -> None:
        exporter = JsonlTraceExporter(path)
        await exporter.bind(turn)
        await exporter.emit("turn_start")
        await exporter.emit("model_call", duration_ms=12)
        await exporter.emit("tool_call", name="read_file", duration_ms=5)
        await exporter.emit("turn_end", status="completed")

    asyncio.run(scenario())

    result = runner.invoke(app, ["trace", "--trace", str(path), "--limit", "1"])

    assert result.exit_code == 0, result.output
    assert "events 4" in result.output
    assert "model_call 1" in result.output
    assert "model_ms 12" in result.output
    assert "tool_ms 5" in result.output
    assert '"event":"turn_end"' in result.output


def test_trace_reports_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")

    result = runner.invoke(app, ["trace", "--trace", str(path)])

    assert result.exit_code == 0, result.output
    assert "no events" in result.output


def test_chat_runs_turns_until_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BALE_PROVIDER", "fake")
    db = tmp_path / "sessions.db"
    session_id = uuid4()

    result = runner.invoke(
        app,
        [
            "chat",
            "--workspace",
            str(tmp_path),
            "--no-shadow-git",
            "--session-db",
            str(db),
            "--session-id",
            str(session_id),
        ],
        input="hello\n/exit\n",
    )

    assert result.exit_code == 0, result.output
    assert "Task completed." in result.output

    async def load():
        return await SessionStore(db).list_turns(session_id)

    turns = asyncio.run(load())
    assert [turn.message for turn in turns] == ["hello"]


def test_undo_restores_workspace_from_checkpoint(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.txt").write_bytes(b"one\n")
    checkpoint_dir = tmp_path / "checkpoints" / str(uuid4())

    async def prepare() -> None:
        shadow = ShadowGit(workspace, checkpoint_dir)
        await shadow.init()

    asyncio.run(prepare())
    (workspace / "a.txt").write_bytes(b"two\n")
    (workspace / "b.txt").write_bytes(b"new\n")

    result = runner.invoke(
        app,
        [
            "undo",
            "--workspace",
            str(workspace),
            "--session-id",
            str(uuid4()),
            "--checkpoint-dir",
            str(checkpoint_dir),
            "--yes",
        ],
    )

    assert result.exit_code == 0, result.output
    assert (workspace / "a.txt").read_bytes() == b"one\n"
    assert not (workspace / "b.txt").exists()


def test_undo_requires_a_checkpoint_directory(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["undo", "--workspace", str(tmp_path), "--session-id", str(uuid4())],
    )

    assert result.exit_code != 0


def _prepare_checkpoint(workspace: Path, checkpoint_dir: Path) -> None:
    async def prepare() -> None:
        shadow = ShadowGit(workspace, checkpoint_dir)
        await shadow.init()
        (workspace / "a.txt").write_bytes(b"two\n")
        await shadow.snapshot("first change")

    asyncio.run(prepare())


def test_checkpoint_list_prints_snapshots(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.txt").write_bytes(b"one\n")
    checkpoint_dir = tmp_path / "checkpoints" / str(uuid4())
    _prepare_checkpoint(workspace, checkpoint_dir)

    result = runner.invoke(
        app,
        [
            "checkpoint",
            "list",
            "--workspace",
            str(workspace),
            "--session-id",
            str(uuid4()),
            "--checkpoint-dir",
            str(checkpoint_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "2 snapshot(s)" in result.output
    assert "first change" in result.output
    assert "bale: baseline" in result.output


def test_checkpoint_list_json_is_machine_readable(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.txt").write_bytes(b"one\n")
    checkpoint_dir = tmp_path / "checkpoints" / str(uuid4())
    _prepare_checkpoint(workspace, checkpoint_dir)

    result = runner.invoke(
        app,
        [
            "checkpoint",
            "list",
            "--workspace",
            str(workspace),
            "--session-id",
            str(uuid4()),
            "--checkpoint-dir",
            str(checkpoint_dir),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [item["message"] for item in payload] == ["first change", "bale: baseline"]
    assert all(len(item["commit"]) == 40 for item in payload)
    assert all(item["created_at"] for item in payload)


def test_checkpoint_list_without_checkpoint(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    result = runner.invoke(
        app,
        [
            "checkpoint",
            "list",
            "--workspace",
            str(workspace),
            "--session-id",
            str(uuid4()),
            "--checkpoint-dir",
            str(tmp_path / "missing"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "no checkpoints" in result.output


def test_checkpoint_clean_removes_snapshots(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.txt").write_bytes(b"one\n")
    checkpoint_dir = tmp_path / "checkpoints" / str(uuid4())
    _prepare_checkpoint(workspace, checkpoint_dir)

    result = runner.invoke(
        app,
        [
            "checkpoint",
            "clean",
            "--workspace",
            str(workspace),
            "--session-id",
            str(uuid4()),
            "--checkpoint-dir",
            str(checkpoint_dir),
            "--yes",
        ],
    )

    assert result.exit_code == 0, result.output
    assert not checkpoint_dir.exists()
    assert (workspace / "a.txt").read_bytes() == b"two\n"


def test_checkpoint_clean_requires_confirmation(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.txt").write_bytes(b"one\n")
    checkpoint_dir = tmp_path / "checkpoints" / str(uuid4())
    _prepare_checkpoint(workspace, checkpoint_dir)

    result = runner.invoke(
        app,
        [
            "checkpoint",
            "clean",
            "--workspace",
            str(workspace),
            "--session-id",
            str(uuid4()),
            "--checkpoint-dir",
            str(checkpoint_dir),
        ],
        input="n\n",
    )

    assert result.exit_code == 1
    assert checkpoint_dir.exists()
