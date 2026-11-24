import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from bale_harness.agent.messages import ModelResponse, ToolCall
from bale_harness.agent.tools.mcp import McpConfigError
from bale_harness.cli import app
from bale_harness.http import create_app
from bale_harness.providers import FakeProvider
from bale_harness.settings import Settings

runner = CliRunner()
SERVER = Path(__file__).resolve().parents[1] / "examples" / "mcp" / "write_server.py"


def _write_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "servers.json"
    config_path.write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "name": "notes",
                        "command": sys.executable,
                        "args": [str(SERVER)],
                        "cwd": str(tmp_path),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return config_path


def _note_call(content: str) -> ModelResponse:
    return ModelResponse(
        tool_calls=[
            ToolCall(
                call_id="",
                name="mcp_notes_write_file",
                arguments={"content": content},
            )
        ]
    )


def test_chat_keeps_mcp_session_across_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = FakeProvider(
        [
            _note_call("first"),
            ModelResponse(content="first done"),
            _note_call("second"),
            ModelResponse(content="second done"),
        ]
    )
    monkeypatch.setattr("bale_harness.cli.create_provider", lambda _settings: provider)
    config_path = _write_config(tmp_path)

    result = runner.invoke(
        app,
        [
            "chat",
            "--workspace",
            str(tmp_path),
            "--no-shadow-git",
            "--yes",
            "--mcp-config",
            str(config_path),
        ],
        input="first\nsecond\n/exit\n",
    )

    assert result.exit_code == 0, result.output
    assert "first done" in result.output
    assert "second done" in result.output
    notes = list((tmp_path / "output").glob("note_*.txt"))
    # Both turns hit the same server; the latest write proves turn two worked.
    assert len(notes) == 1
    assert notes[0].read_text(encoding="utf-8") == "second"


def test_chat_rejects_broken_mcp_config(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "chat",
            "--workspace",
            str(tmp_path),
            "--no-shadow-git",
            "--mcp-config",
            str(broken),
        ],
        input="/exit\n",
    )

    assert result.exit_code != 0
    assert "invalid" in result.output.lower() or "mcp" in result.output.lower()


def test_http_run_registers_configured_mcp_tools(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    settings = Settings(
        _env_file=None,
        provider="fake",
        mcp_config=config_path,
    )
    provider = FakeProvider([_note_call("hello http"), ModelResponse(content="done")])
    app_instance = create_app(settings=settings, provider=provider)

    with TestClient(app_instance) as client:
        response = client.post(
            "/v1/runs",
            json={
                "message": "write a note",
                "workspace": str(tmp_path),
                "shadow_git": False,
                "approve_writes": True,
            },
        )

    assert response.status_code == 200, response.text
    assert response.json()["answer"] == "done"
    notes = list((tmp_path / "output").glob("note_*.txt"))
    assert len(notes) == 1
    assert notes[0].read_text(encoding="utf-8") == "hello http"


def test_http_app_rejects_broken_mcp_config(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")

    with pytest.raises(McpConfigError):
        create_app(
            settings=Settings(
                _env_file=None,
                provider="fake",
                mcp_config=broken,
            ),
            provider=FakeProvider(),
        )
