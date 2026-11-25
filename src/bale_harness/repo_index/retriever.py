"""Lazy workspace retrieval that formats excerpts for the context window.

The index is built on first use, so constructing a retriever per request costs
nothing until a turn actually starts. Excerpts are plain text labeled with
``path:start-end`` so the model can cite or re-read them with tools.

Without an embedder the retriever is purely lexical. Supplying an
:class:`~bale_harness.repo_index.embedding.Embedder` switches it to hybrid
BM25 plus vector ranking; embedding failures fall back to BM25.
"""

from pathlib import Path

from bale_harness.repo_index.embedding import Embedder, create_embedder
from bale_harness.repo_index.hybrid import DEFAULT_ALPHA, HybridIndex
from bale_harness.settings import Settings

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
        embedder: Embedder | None = None,
        alpha: float = DEFAULT_ALPHA,
        index_path: Path | None = None,
    ) -> None:
        if top_k <= 0:
            raise ValueError("top_k must be positive")
        if max_chars <= 0:
            raise ValueError("max_chars must be positive")
        if not 0.0 <= alpha <= 1.0:
            raise ValueError("alpha must be between 0 and 1")
        self._root = Path(root)
        self._top_k = top_k
        self._max_chars = max_chars
        self._embedder = embedder
        self._alpha = alpha
        self._index_path = index_path
        self._index: HybridIndex | None = None

    def _index_or_build(self) -> HybridIndex:
        if self._index is None:
            self._index = HybridIndex(
                self._root,
                embedder=self._embedder,
                alpha=self._alpha,
                index_path=self._index_path,
            )
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


def create_retriever(settings: Settings, root: Path) -> Bm25Retriever | None:
    """Build the retriever configured by ``settings``; ``None`` when disabled."""
    if not settings.retrieval_enabled:
        return None
    return Bm25Retriever(
        root,
        top_k=settings.retrieval_top_k,
        embedder=create_embedder(settings),
        alpha=settings.retrieval_alpha,
        index_path=settings.retrieval_index_path,
    )
