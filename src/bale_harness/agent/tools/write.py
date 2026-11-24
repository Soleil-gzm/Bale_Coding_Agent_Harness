"""Write and edit tools.

Both tools mutate the workspace, so they are registered as ``write`` side
effects and therefore require approval. Writes are atomic: content goes to a
temporary file in the target directory and is moved into place, so a failure
never leaves a half-written file.
"""

import os
import tempfile
import time
from pathlib import Path
from typing import Any

from bale_harness.agent.messages import ToolSpec
from bale_harness.agent.security import WorkspaceError, WorkspaceGuard
from bale_harness.agent.tools.common import error, success
from bale_harness.agent.tools.registry import BaseTool
from bale_harness.contracts import ToolResult


def _atomic_write(path: Path, data: bytes) -> None:
    handle, temp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".bale-", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _resolve(
    guard: WorkspaceGuard, rel: str, tool: str, start: float
) -> Path | ToolResult:
    """Resolve a workspace path, returning an error result when it escapes."""
    try:
        return guard.resolve(rel)
    except WorkspaceError:
        return error(tool, f"path escapes workspace: {rel}", "path_escape", start)


class WriteFileTool(BaseTool):
    spec = ToolSpec(
        name="write_file",
        description="Create or overwrite a UTF-8 text file in the workspace.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
        side_effect="write",
        timeout_ms=10_000,
    )

    def __init__(self, guard: WorkspaceGuard) -> None:
        self._guard = guard

    def validate_arguments(self, arguments: dict[str, Any]) -> None:
        path = arguments.get("path")
        if not isinstance(path, str) or not path:
            raise ValueError("path must be a non-empty string")
        if not isinstance(arguments.get("content"), str):
            raise ValueError("content must be a string")

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        start = time.perf_counter()
        rel = arguments["path"]
        content = arguments["content"]
        resolved = _resolve(self._guard, rel, "write_file", start)
        if isinstance(resolved, ToolResult):
            return resolved

        if resolved.exists() and not resolved.is_file():
            return error("write_file", f"not a file: {rel}", "not_a_file", start)

        created = not resolved.exists()
        data = content.encode("utf-8")
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(resolved, data)
        except OSError as exc:
            return error("write_file", f"write failed: {exc}", "write_failed", start)

        return success(
            "write_file",
            f"wrote {len(data)} bytes to {rel}",
            metadata={"path": rel, "bytes": len(data), "created": created},
            start=start,
        )


class EditFileTool(BaseTool):
    spec = ToolSpec(
        name="edit_file",
        description=(
            "Replace an exact substring in a workspace file. By default the "
            "substring must occur exactly once; set replace_all to replace "
            "every occurrence."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
                "replace_all": {"type": "boolean"},
            },
            "required": ["path", "old_string", "new_string"],
        },
        side_effect="write",
        timeout_ms=10_000,
    )

    def __init__(self, guard: WorkspaceGuard) -> None:
        self._guard = guard

    def validate_arguments(self, arguments: dict[str, Any]) -> None:
        path = arguments.get("path")
        if not isinstance(path, str) or not path:
            raise ValueError("path must be a non-empty string")
        old_string = arguments.get("old_string")
        if not isinstance(old_string, str) or not old_string:
            raise ValueError("old_string must be a non-empty string")
        if not isinstance(arguments.get("new_string"), str):
            raise ValueError("new_string must be a string")
        if "replace_all" in arguments and not isinstance(
            arguments["replace_all"], bool
        ):
            raise ValueError("replace_all must be a boolean")

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        start = time.perf_counter()
        rel = arguments["path"]
        old_string = arguments["old_string"]
        new_string = arguments["new_string"]
        replace_all = bool(arguments.get("replace_all", False))

        resolved = _resolve(self._guard, rel, "edit_file", start)
        if isinstance(resolved, ToolResult):
            return resolved

        if not resolved.exists():
            return error("edit_file", f"file not found: {rel}", "not_found", start)
        if not resolved.is_file():
            return error("edit_file", f"not a file: {rel}", "not_a_file", start)

        # Read and write raw bytes so CRLF files keep their line endings.
        try:
            original = resolved.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            return error(
                "edit_file", f"file is not valid UTF-8: {rel}", "decode_error", start
            )

        occurrences = original.count(old_string)
        if occurrences == 0:
            return error(
                "edit_file",
                f"old_string not found in {rel}",
                "precondition_failed",
                start,
            )
        if occurrences > 1 and not replace_all:
            return error(
                "edit_file",
                f"old_string occurs {occurrences} times in {rel}; "
                "provide more context or set replace_all=true",
                "precondition_failed",
                start,
            )

        replacements = occurrences if replace_all else 1
        updated = original.replace(old_string, new_string, -1 if replace_all else 1)
        try:
            _atomic_write(resolved, updated.encode("utf-8"))
        except OSError as exc:
            return error("edit_file", f"write failed: {exc}", "write_failed", start)

        return success(
            "edit_file",
            f"replaced {replacements} occurrence(s) in {rel}",
            metadata={"path": rel, "replacements": replacements},
            start=start,
        )


def build_write_tools(guard: WorkspaceGuard) -> list[BaseTool]:
    """Return the workspace-mutating tools wired to ``guard``."""
    return [WriteFileTool(guard), EditFileTool(guard)]
