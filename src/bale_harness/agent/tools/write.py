"""Write, edit, move, delete, and patch tools.

All tools here mutate the workspace, so they are registered as ``write`` side
effects and therefore require approval. Writes are atomic: content goes to a
temporary file in the target directory and is moved into place, so a failure
never leaves a half-written file.
"""

import os
import tempfile
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

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


class MoveFileArgs(BaseModel):
    """Arguments for :class:`MoveFileTool`."""

    source: str = Field(min_length=1)
    destination: str = Field(min_length=1)


class MoveFileTool(BaseTool):
    spec = ToolSpec(
        name="move_file",
        description=(
            "Move a workspace file to a new workspace-relative path. Parent "
            "directories of the destination are created; an existing "
            "destination is rejected instead of being overwritten."
        ),
        parameters={
            "type": "object",
            "properties": {
                "source": {"type": "string"},
                "destination": {"type": "string"},
            },
            "required": ["source", "destination"],
        },
        side_effect="write",
        timeout_ms=10_000,
    )

    def __init__(self, guard: WorkspaceGuard) -> None:
        self._guard = guard

    def validate_arguments(self, arguments: dict[str, Any]) -> None:
        MoveFileArgs.model_validate(arguments)

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        start = time.perf_counter()
        parsed = MoveFileArgs.model_validate(arguments)
        source = parsed.source
        destination = parsed.destination

        resolved_source = _resolve(self._guard, source, "move_file", start)
        if isinstance(resolved_source, ToolResult):
            return resolved_source
        resolved_dest = _resolve(self._guard, destination, "move_file", start)
        if isinstance(resolved_dest, ToolResult):
            return resolved_dest

        if not resolved_source.exists():
            return error("move_file", f"source not found: {source}", "not_found", start)
        if resolved_dest.exists():
            return error(
                "move_file",
                f"destination already exists: {destination}",
                "already_exists",
                start,
            )

        try:
            resolved_dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(resolved_source, resolved_dest)
        except OSError as exc:
            return error("move_file", f"move failed: {exc}", "move_failed", start)

        return success(
            "move_file",
            f"moved {source} to {destination}",
            metadata={"source": source, "destination": destination},
            start=start,
        )


class DeleteFileArgs(BaseModel):
    """Arguments for :class:`DeleteFileTool`."""

    path: str = Field(min_length=1)


class DeleteFileTool(BaseTool):
    spec = ToolSpec(
        name="delete_file",
        description="Delete a single UTF-8 file from the workspace.",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        side_effect="write",
        timeout_ms=10_000,
    )

    def __init__(self, guard: WorkspaceGuard) -> None:
        self._guard = guard

    def validate_arguments(self, arguments: dict[str, Any]) -> None:
        DeleteFileArgs.model_validate(arguments)

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        start = time.perf_counter()
        rel = DeleteFileArgs.model_validate(arguments).path

        resolved = _resolve(self._guard, rel, "delete_file", start)
        if isinstance(resolved, ToolResult):
            return resolved

        if not resolved.exists():
            return error("delete_file", f"file not found: {rel}", "not_found", start)
        if not resolved.is_file():
            return error("delete_file", f"not a file: {rel}", "not_a_file", start)

        try:
            resolved.unlink()
        except OSError as exc:
            return error("delete_file", f"delete failed: {exc}", "delete_failed", start)

        return success(
            "delete_file", f"deleted {rel}", metadata={"path": rel}, start=start
        )


class PatchReplacement(BaseModel):
    """A single exact-text replacement inside a patch."""

    old: str = Field(min_length=1)
    new: str


class ApplyPatchArgs(BaseModel):
    """Arguments for :class:`ApplyPatchTool`."""

    path: str = Field(min_length=1)
    replacements: list[PatchReplacement] = Field(min_length=1)


class ApplyPatchTool(BaseTool):
    spec = ToolSpec(
        name="apply_patch",
        description=(
            "Apply one or more exact text replacements to a workspace file. "
            "Each old text must occur exactly once, replacements are applied "
            "in order, and the file is written only if every replacement "
            "succeeds."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "replacements": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "old": {"type": "string"},
                            "new": {"type": "string"},
                        },
                        "required": ["old", "new"],
                    },
                },
            },
            "required": ["path", "replacements"],
        },
        side_effect="write",
        timeout_ms=10_000,
    )

    def __init__(self, guard: WorkspaceGuard) -> None:
        self._guard = guard

    def validate_arguments(self, arguments: dict[str, Any]) -> None:
        ApplyPatchArgs.model_validate(arguments)

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        start = time.perf_counter()
        try:
            parsed = ApplyPatchArgs.model_validate(arguments)
        except ValueError as exc:
            return error("apply_patch", str(exc), "invalid_arguments", start)

        rel = parsed.path
        resolved = _resolve(self._guard, rel, "apply_patch", start)
        if isinstance(resolved, ToolResult):
            return resolved

        if not resolved.exists():
            return error("apply_patch", f"file not found: {rel}", "not_found", start)
        if not resolved.is_file():
            return error("apply_patch", f"not a file: {rel}", "not_a_file", start)

        # Read and write raw bytes so CRLF files keep their line endings.
        try:
            original = resolved.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            return error(
                "apply_patch", f"file is not valid UTF-8: {rel}", "decode_error", start
            )

        updated = original
        for index, replacement in enumerate(parsed.replacements):
            occurrences = updated.count(replacement.old)
            if occurrences != 1:
                return error(
                    "apply_patch",
                    f"replacement {index}: old text occurs {occurrences} times "
                    f"in {rel}; expected exactly once",
                    "precondition_failed",
                    start,
                )
            updated = updated.replace(replacement.old, replacement.new, 1)

        try:
            _atomic_write(resolved, updated.encode("utf-8"))
        except OSError as exc:
            return error("apply_patch", f"write failed: {exc}", "write_failed", start)

        return success(
            "apply_patch",
            f"applied {len(parsed.replacements)} replacement(s) to {rel}",
            metadata={"path": rel, "replacements": len(parsed.replacements)},
            start=start,
        )


def build_write_tools(guard: WorkspaceGuard) -> list[BaseTool]:
    """Return the workspace-mutating tools wired to ``guard``."""
    return [
        WriteFileTool(guard),
        EditFileTool(guard),
        MoveFileTool(guard),
        DeleteFileTool(guard),
        ApplyPatchTool(guard),
    ]
