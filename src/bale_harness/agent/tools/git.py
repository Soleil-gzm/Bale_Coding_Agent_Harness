"""Workspace diff inspection and the explicit task-completion signal."""

import time
from typing import Any

from bale_harness.agent.checkpoint import ShadowGit, ShadowGitError
from bale_harness.agent.messages import ToolSpec
from bale_harness.agent.tools.common import MAX_OUTPUT_CHARS, error, success, truncate
from bale_harness.agent.tools.registry import BaseTool
from bale_harness.contracts import ToolResult


class GitDiffTool(BaseTool):
    spec = ToolSpec(
        name="git_diff",
        description=(
            "Show the unified diff of workspace changes against the task baseline."
        ),
        parameters={"type": "object", "properties": {}, "required": []},
        concurrency_safe=True,
        timeout_ms=30_000,
    )

    def __init__(self, checkpoint: ShadowGit | None) -> None:
        self._checkpoint = checkpoint

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        start = time.perf_counter()
        if self._checkpoint is None:
            return error(
                "git_diff",
                "shadow git is disabled for this run",
                "checkpoint_unavailable",
                start,
            )
        try:
            diff = await self._checkpoint.diff()
        except ShadowGitError as exc:
            return error(
                "git_diff", f"checkpoint failure: {exc}", "checkpoint_error", start
            )

        content, truncated = truncate(diff, MAX_OUTPUT_CHARS)
        return success(
            "git_diff",
            content or "(no changes)",
            metadata={"bytes": len(diff)},
            truncated=truncated,
            start=start,
        )


class FinishTool(BaseTool):
    spec = ToolSpec(
        name="finish",
        description="Signal that the task is complete and summarize the result.",
        parameters={
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
        concurrency_safe=True,
        timeout_ms=5_000,
    )

    def validate_arguments(self, arguments: dict[str, Any]) -> None:
        message = arguments.get("message")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must be a non-empty string")

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        start = time.perf_counter()
        message = arguments["message"]
        return success("finish", message, metadata={"finished": True}, start=start)


def build_git_tools(checkpoint: ShadowGit | None) -> list[BaseTool]:
    """Return diff and completion tools; diff degrades when shadow git is off."""
    return [GitDiffTool(checkpoint), FinishTool()]
