import asyncio
from pathlib import Path
from typing import Any

import pytest

from bale_harness.agent.checkpoint import ShadowGit
from bale_harness.agent.tools.git import FinishTool, GitDiffTool


def _run(tool: Any, arguments: dict[str, Any]):
    return asyncio.run(tool.execute(arguments))


def test_git_diff_without_checkpoint_is_unavailable(tmp_path: Path) -> None:
    result = _run(GitDiffTool(None), {})

    assert result.status == "error"
    assert result.error_code == "checkpoint_unavailable"


def test_git_diff_reports_workspace_changes(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"one\n")
    shadow = ShadowGit(tmp_path)

    async def scenario():
        await shadow.init()
        (tmp_path / "a.txt").write_bytes(b"two\n")
        try:
            return await GitDiffTool(shadow).execute({})
        finally:
            await shadow.cleanup()

    result = asyncio.run(scenario())

    assert result.status == "success"
    assert "-one" in result.content
    assert "+two" in result.content


def test_finish_echoes_message() -> None:
    result = _run(FinishTool(), {"message": "all done"})

    assert result.status == "success"
    assert result.content == "all done"
    assert result.metadata["finished"] is True


def test_finish_requires_message() -> None:
    with pytest.raises(ValueError, match="message"):
        FinishTool().validate_arguments({"message": "   "})
