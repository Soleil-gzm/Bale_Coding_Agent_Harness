"""Lazy BM25 retrieval that formats excerpts for the context window.

The retriever builds its index on first use, so constructing one per request
costs nothing until a turn actually starts. Excerpts are plain text labeled
with ``path:start-end`` so the model can cite or re-read them with tools.
"""

from pathlib import Path

from bale_harness.repo_index.index import Bm25Index

DEFAULT_TOP_K = 5
DEFAULT_MAX_CHARS = 4000


class Bm25Retriever:
    """Rank workspace chunks for a query and render a bounded excerpt list."""

    def __init__(
        self,
        root: Path,
        *,
        top_k: int = DEFAULT_TOP_K,
        max_chars: int = DEFAULT_MAX_CHARS,
    ) -> None:
        if top_k <= 0:
            raise ValueError("top_k must be positive")
        if max_chars <= 0:
            raise ValueError("max_chars must be positive")
        self._root = Path(root)
        self._top_k = top_k
        self._max_chars = max_chars
        self._index: Bm25Index | None = None

    def _index_or_build(self) -> Bm25Index:
        if self._index is None:
            self._index = Bm25Index.from_directory(self._root)
        return self._index

    def retrieve(self, query: str) -> list[str]:
        """Return formatted ``path:start-end`` excerpts for ``query``."""
        excerpts: list[str] = []
        total = 0
        for hit in self._index_or_build().search(query, self._top_k):
            block = f"{hit.path}:{hit.start_line}-{hit.end_line}\n{hit.snippet}"
            if excerpts and total + len(block) > self._max_chars:
                break
            excerpts.append(block)
            total += len(block)
        return excerpts
