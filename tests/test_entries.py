import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from bale_harness.cli import app
from bale_harness.http import create_app
from bale_harness.turns import TurnRequest, build_turn

runner = CliRunner()


def turn_input(workspace: Path) -> dict[str, object]:
    return {
        "message": "  Inspect the repository\n",
        "workspace": str(workspace),
        "repo_revision": "abc123",
        "max_iterations": 8,
        "token_budget": 4096,
        "cost_budget": 1.25,
        "approval_mode": "approve_all",
    }


def test_build_turn_always_creates_turn(tmp_path: Path) -> None:
    turn = build_turn(TurnRequest(**turn_input(tmp_path)))

    assert turn.status == "created"
    assert turn.message == "Inspect the repository"
    assert turn.workspace == tmp_path


def test_cli_and_http_construct_equivalent_turns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BALE_PROVIDER", "fake")
    values = turn_input(tmp_path)
    cli_result = runner.invoke(
        app,
        [
            "turn",
            "--message",
            str(values["message"]),
            "--workspace",
            str(values["workspace"]),
            "--repo-revision",
            str(values["repo_revision"]),
            "--max-iterations",
            str(values["max_iterations"]),
            "--token-budget",
            str(values["token_budget"]),
            "--cost-budget",
            str(values["cost_budget"]),
            "--approval-mode",
            str(values["approval_mode"]),
        ],
    )

    assert cli_result.exit_code == 0, cli_result.output
    cli_payload = json.loads(cli_result.stdout)

    with TestClient(create_app()) as client:
        response = client.post("/v1/turns", json=values)

    assert response.status_code == 201
    http_payload = response.json()
    assert cli_payload.pop("status") == "created"
    assert http_payload.pop("status") == "created"
    assert cli_payload.pop("turn_id") != http_payload.pop("turn_id")
    assert cli_payload.pop("session_id") != http_payload.pop("session_id")
    assert cli_payload == http_payload


@pytest.mark.parametrize(
    "override",
    [
        {"message": "   "},
        {"token_budget": 0},
        {"unexpected": True},
    ],
)
def test_http_rejects_invalid_turn_input(
    override: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BALE_PROVIDER", "fake")
    values = turn_input(tmp_path) | override

    with TestClient(create_app()) as client:
        response = client.post("/v1/turns", json=values)

    assert response.status_code == 422


def test_mcp_example_compiles() -> None:
    source = Path(__file__).parents[1] / "examples" / "mcp" / "write_server.py"

    compile(source.read_text(encoding="utf-8"), str(source), "exec")
