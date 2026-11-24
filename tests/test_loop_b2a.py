import asyncio
from pathlib import Path

from bale_harness.agent.approval import auto_approve
from bale_harness.agent.checkpoint import ShadowGit
from bale_harness.agent.loop import build_default_loop
from bale_harness.agent.messages import ModelResponse, ToolCall
from bale_harness.contracts import Turn
from bale_harness.providers import FakeProvider


def _turn(workspace: Path, max_iterations: int = 6) -> Turn:
    return Turn(
        message="Add add() with a test and run it",
        workspace=workspace,
        token_budget=4096,
        max_iterations=max_iterations,
    )


def test_write_run_tests_finish_closes_the_loop(tmp_path: Path) -> None:
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        call_id="",
                        name="write_file",
                        arguments={
                            "path": "app.py",
                            "content": "def add(a, b):\n    return a + b\n",
                        },
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        call_id="",
                        name="write_file",
                        arguments={
                            "path": "test_app.py",
                            "content": (
                                "from app import add\n\n\n"
                                "def test_add():\n"
                                "    assert add(1, 2) == 3\n"
                            ),
                        },
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(call_id="", name="run_tests", arguments={"path": "."})
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        call_id="",
                        name="finish",
                        arguments={"message": "added add() and its passing test"},
                    )
                ]
            ),
        ]
    )
    checkpoint = ShadowGit(tmp_path)
    loop = build_default_loop(
        provider,
        tmp_path,
        approver=auto_approve,
        checkpoint=checkpoint,
    )

    async def scenario():
        try:
            return await loop.run(_turn(tmp_path))
        finally:
            await checkpoint.cleanup()

    result = asyncio.run(scenario())

    assert result.status == "completed"
    assert result.answer == "added add() and its passing test"
    assert result.iterations == 4
    assert result.tool_calls == 4
    assert result.diff is not None
    assert "app.py" in result.diff
    assert "test_app.py" in result.diff
    assert len(result.checkpoints) == 2
    assert (tmp_path / "app.py").exists()
    assert (tmp_path / "test_app.py").exists()
    assert not (tmp_path / "__pycache__").exists()


def test_denied_write_leaves_workspace_unchanged(tmp_path: Path) -> None:
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        call_id="",
                        name="write_file",
                        arguments={"path": "secret.txt", "content": "x"},
                    )
                ]
            ),
            ModelResponse(content="cannot write without approval"),
        ]
    )
    loop = build_default_loop(provider, tmp_path, approval_mode="auto_read")

    result = asyncio.run(loop.run(_turn(tmp_path)))

    assert result.status == "completed"
    assert result.answer == "cannot write without approval"
    assert not (tmp_path / "secret.txt").exists()
    assert result.diff is None
