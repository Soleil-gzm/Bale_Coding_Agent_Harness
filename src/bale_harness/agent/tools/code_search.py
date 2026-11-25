"""Code search tool for the workspace, lexical with optional vectors."""

import time
from pathlib import Path
from typing import Any

from bale_harness.agent.messages import ToolSpec
from bale_harness.agent.security import WorkspaceError, WorkspaceGuard
from bale_harness.agent.tools.common import (
    MAX_OUTPUT_CHARS,
    error,
    success,
    truncate,
)
from bale_harness.agent.tools.registry import BaseTool
from bale_harness.contracts import ToolResult
from bale_harness.repo_index import Embedder, HybridIndex, SearchHit

DEFAULT_LIMIT = 10
MAX_LIMIT = 50


def format_hits(hits: list[SearchHit]) -> str:
    """Render ranked hits as ``path:start-end`` headers with indented snippets."""
    blocks: list[str] = []
    for hit in hits:
        blocks.append(
            f"{hit.path}:{hit.start_line}-{hit.end_line} score={hit.score:.3f}"
        )
        blocks.extend(f"  {line}" for line in hit.snippet.splitlines())
    return "\n".join(blocks)


class SearchCodeTool(BaseTool):
    """Rank workspace code chunks against a query with a hybrid index."""

    spec = ToolSpec(
        name="search_code",
        description=(
            "Rank workspace code chunks for a text or identifier query with "
            "BM25 and optional vector recall; returns path, line range, "
            "score, and snippet."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT},
                "path": {"type": "string"},
            },
            "required": ["query"],
        },
        concurrency_safe=True,
        timeout_ms=60_000,
    )

    def __init__(
        self,
        guard: WorkspaceGuard,
        *,
        embedder: Embedder | None = None,
        alpha: float = 0.5,
        index_path: Path | None = None,
    ) -> None:
        self._guard = guard
        self._embedder = embedder
        self._alpha = alpha
        self._index_path = index_path
        self._indexes: dict[Path, HybridIndex] = {}

    def validate_arguments(self, arguments: dict[str, Any]) -> None:
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        limit = arguments.get("limit", DEFAULT_LIMIT)
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("limit must be an integer")
        if not 1 <= limit <= MAX_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
        path = arguments.get("path", ".")
        if not isinstance(path, str):
            raise ValueError("path must be a string")

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        start = time.perf_counter()
        query = arguments["query"]
        limit = arguments.get("limit", DEFAULT_LIMIT)
        rel = arguments.get("path", ".") or "."
        try:
            root = self._guard.resolve(rel)
        except WorkspaceError:
            return error(
                "search_code", f"path escapes workspace: {rel}", "path_escape", start
            )
        if not root.exists():
            return error("search_code", f"path not found: {rel}", "not_found", start)
        if not root.is_dir():
            return error(
                "search_code", f"not a directory: {rel}", "not_a_directory", start
            )

        index = self._indexes.get(root)
        if index is None:
            index = HybridIndex(
                root,
                embedder=self._embedder,
                alpha=self._alpha,
                index_path=self._index_path,
            )
            self._indexes[root] = index

        hits = index.search(query, limit)
        content = format_hits(hits) if hits else f"no matches for {query.strip()!r}"
        content, output_truncated = truncate(content, MAX_OUTPUT_CHARS)
        return success(
            "search_code",
            content,
            metadata={
                "query": query,
                "path": rel,
                "hits": len(hits),
                "indexed_files": index.file_count,
                "indexed_chunks": index.chunk_count,
                "vector_search": index.vector_enabled,
            },
            truncated=output_truncated,
            start=start,
        )
