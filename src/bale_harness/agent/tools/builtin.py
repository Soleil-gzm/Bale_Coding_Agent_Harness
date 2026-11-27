"""Built-in read-only tools: list, read, and search files in the workspace."""

import re
import time
from pathlib import Path
from typing import Any

from bale_harness.agent.messages import ToolSpec
from bale_harness.agent.security import WorkspaceError, WorkspaceGuard
from bale_harness.agent.tools.code_search import SearchCodeTool
from bale_harness.agent.tools.common import (
    MAX_OUTPUT_CHARS,
    error,
    success,
    truncate,
)
from bale_harness.agent.tools.registry import BaseTool
from bale_harness.contracts import ToolResult
from bale_harness.repo_index import Embedder

MAX_READ_BYTES = 64 * 1024
MAX_LIST_ENTRIES = 500
MAX_SEARCH_MATCHES = 100


class ReadFileTool(BaseTool):
    spec = ToolSpec(
        name="read_file",
        description="Read a UTF-8 text file from the workspace, bounded to 64 KiB.",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        concurrency_safe=True,
        timeout_ms=10_000,
    )

    def __init__(self, guard: WorkspaceGuard) -> None:
        self._guard = guard

    def validate_arguments(self, arguments: dict[str, Any]) -> None:
        path = arguments.get("path")
        if not isinstance(path, str) or not path:
            raise ValueError("path must be a non-empty string")

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        start = time.perf_counter()
        rel = arguments["path"]
        try:
            resolved = self._guard.resolve(rel)
        except WorkspaceError:
            return error(
                "read_file", f"path escapes workspace: {rel}", "path_escape", start
            )
        if not resolved.exists():
            return error("read_file", f"file not found: {rel}", "not_found", start)
        if not resolved.is_file():
            return error("read_file", f"not a file: {rel}", "not_a_file", start)

        total = resolved.stat().st_size
        data = resolved.read_bytes()[:MAX_READ_BYTES]
        content = data.decode("utf-8", errors="replace")
        truncated = total > MAX_READ_BYTES
        return success(
            "read_file",
            content,
            metadata={"path": rel, "bytes": len(data)},
            truncated=truncated,
            start=start,
        )


class ListFilesTool(BaseTool):
    spec = ToolSpec(
        name="list_files",
        description="List files and directories under a workspace path.",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": [],
        },
        concurrency_safe=True,
        timeout_ms=10_000,
    )

    def __init__(self, guard: WorkspaceGuard) -> None:
        self._guard = guard

    def validate_arguments(self, arguments: dict[str, Any]) -> None:
        path = arguments.get("path", ".")
        if not isinstance(path, str):
            raise ValueError("path must be a string")

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        start = time.perf_counter()
        rel = arguments.get("path", ".") or "."
        try:
            resolved = self._guard.resolve(rel)
        except WorkspaceError:
            return error(
                "list_files", f"path escapes workspace: {rel}", "path_escape", start
            )
        if not resolved.exists():
            return error("list_files", f"path not found: {rel}", "not_found", start)
        if not resolved.is_dir():
            return error(
                "list_files", f"not a directory: {rel}", "not_a_directory", start
            )

        entries = sorted(resolved.iterdir(), key=lambda p: p.name)
        lines: list[str] = []
        truncated = False
        for entry in entries:
            if len(lines) >= MAX_LIST_ENTRIES:
                truncated = True
                break
            suffix = "/" if entry.is_dir() else ""
            lines.append(f"{entry.name}{suffix}")
        content, output_truncated = truncate("\n".join(lines), MAX_OUTPUT_CHARS)
        return success(
            "list_files",
            content,
            metadata={"path": rel, "count": len(lines)},
            truncated=truncated or output_truncated,
            start=start,
        )


class SearchFilesTool(BaseTool):
    spec = ToolSpec(
        name="search_files",
        description="Search file contents with a regular expression under a path.",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
            },
            "required": ["pattern"],
        },
        concurrency_safe=True,
        timeout_ms=30_000,
    )

    def __init__(self, guard: WorkspaceGuard) -> None:
        self._guard = guard

    def validate_arguments(self, arguments: dict[str, Any]) -> None:
        pattern = arguments.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            raise ValueError("pattern must be a non-empty string")
        try:
            re.compile(pattern)
        except re.error as error:
            raise ValueError(f"invalid regex: {error}") from error
        path = arguments.get("path", ".")
        if not isinstance(path, str):
            raise ValueError("path must be a string")

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        start = time.perf_counter()
        pattern = arguments["pattern"]
        rel = arguments.get("path", ".") or "."
        try:
            root = self._guard.resolve(rel)
        except WorkspaceError:
            return error(
                "search_files", f"path escapes workspace: {rel}", "path_escape", start
            )
        if not root.exists():
            return error("search_files", f"path not found: {rel}", "not_found", start)

        regex = re.compile(pattern)
        matches: list[str] = []
        truncated = False
        for file_path in self._iter_files(root):
            try:
                with file_path.open("r", encoding="utf-8", errors="replace") as handle:
                    for line_no, line in enumerate(handle, start=1):
                        if regex.search(line):
                            if len(matches) >= MAX_SEARCH_MATCHES:
                                truncated = True
                                break
                            rel_display = self._display_path(root, file_path)
                            matches.append(f"{rel_display}:{line_no}: {line.rstrip()}")
                if truncated:
                    break
            except OSError:
                continue
        content, output_truncated = truncate("\n".join(matches), MAX_OUTPUT_CHARS)
        return success(
            "search_files",
            content,
            metadata={"pattern": pattern, "path": rel, "count": len(matches)},
            truncated=truncated or output_truncated,
            start=start,
        )

    def _iter_files(self, root: Path) -> list[Path]:
        files: list[Path] = []
        for candidate in root.rglob("*"):
            if not candidate.is_file():
                continue
            try:
                # Resolve first, then compare against the workspace root: a
                # relative_to(root) check would validate the link path instead
                # of its target when ``path`` points at a subdirectory.
                candidate.resolve().relative_to(self._guard.root)
            except (OSError, RuntimeError, ValueError):
                continue
            files.append(candidate)
        return files

    @staticmethod
    def _display_path(root: Path, file_path: Path) -> str:
        try:
            return str(file_path.relative_to(root)).replace("\\", "/")
        except ValueError:
            return file_path.name


def build_default_tools(
    guard: WorkspaceGuard,
    *,
    search_embedder: Embedder | None = None,
    search_alpha: float = 0.5,
    search_index_path: Path | None = None,
) -> list[BaseTool]:
    """Return the built-in read-only tools wired to the given workspace guard.

    ``search_embedder`` enables hybrid vector ranking for ``search_code``;
    without it the tool stays lexical.
    """
    return [
        ReadFileTool(guard),
        ListFilesTool(guard),
        SearchFilesTool(guard),
        SearchCodeTool(
            guard,
            embedder=search_embedder,
            alpha=search_alpha,
            index_path=search_index_path,
        ),
    ]
