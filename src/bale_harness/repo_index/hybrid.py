"""Hybrid BM25 and vector retrieval with incremental disk snapshots.

The index always computes a lexical BM25 ranking. When an :class:`Embedder` is
available it also embeds each chunk and the query, then blends the two
normalized rankings with ``alpha`` (lexical weight). Any embedding failure
degrades the index to BM25 and is reported through ``vector_error``.

Rebuilding is incremental: files whose ``mtime_ns:size`` signature is
unchanged are reused from the persisted snapshot, changed or new files are
re-chunked, and deleted files are dropped. With ``index_path`` set, the work
survives across processes; otherwise the index stays in memory.
"""

from dataclasses import dataclass
from pathlib import Path

from bale_harness.repo_index.embedding import Embedder, cosine
from bale_harness.repo_index.index import (
    MAX_CHUNKS,
    Bm25Index,
    CodeChunk,
    SearchHit,
    chunk_source,
    iter_source_files,
    read_text_file,
)
from bale_harness.repo_index.store import (
    IndexSnapshot,
    StoredChunk,
    StoredFile,
    load_snapshot,
    save_snapshot,
)

DEFAULT_ALPHA = 0.5


@dataclass
class _FileState:
    """In-memory chunks and vectors for one workspace file."""

    signature: str
    chunks: list[CodeChunk]
    vectors: list[list[float] | None] | None = None


def _signature(path: Path) -> str | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return f"{stat.st_mtime_ns}:{stat.st_size}"


def _stored_chunks(state: _FileState) -> list[StoredChunk]:
    vectors = state.vectors or [None] * len(state.chunks)
    return [
        StoredChunk(
            start_line=chunk.start_line,
            end_line=chunk.end_line,
            text=chunk.text,
            vector=vector,
        )
        for chunk, vector in zip(state.chunks, vectors, strict=True)
    ]


def _clear_vectors(states: list[_FileState]) -> None:
    for state in states:
        state.vectors = None


def _fuse(
    lexical: list[float],
    vector: list[float] | None,
    alpha: float,
) -> list[float]:
    lexical_max = max(lexical, default=0.0)
    lexical_norm = [
        score / lexical_max if lexical_max > 0 else 0.0 for score in lexical
    ]
    if vector is None:
        return lexical_norm
    vector_max = max(vector, default=0.0)
    vector_norm = [score / vector_max if vector_max > 0 else 0.0 for score in vector]
    return [
        alpha * lex + (1.0 - alpha) * vec
        for lex, vec in zip(lexical_norm, vector_norm, strict=True)
    ]


class HybridIndex:
    """Incremental code index that fuses BM25 with optional embeddings."""

    def __init__(
        self,
        root: Path,
        *,
        embedder: Embedder | None = None,
        alpha: float = DEFAULT_ALPHA,
        index_path: Path | None = None,
    ) -> None:
        if not 0.0 <= alpha <= 1.0:
            raise ValueError("alpha must be between 0 and 1")
        self._root = Path(root)
        self._embedder = embedder
        self._alpha = alpha
        self._index_path = Path(index_path) if index_path is not None else None
        self._files: dict[str, _FileState] = {}
        self._chunks: list[CodeChunk] = []
        self._vectors: list[list[float] | None] | None = None
        self._vector_model: str | None = None
        self._bm25 = Bm25Index([], file_count=0)
        self.reused_files = 0
        self.created_files = 0
        self.updated_files = 0
        self.removed_files = 0
        self.vector_error: str | None = None
        self.refresh()

    @property
    def file_count(self) -> int:
        return len(self._files)

    @property
    def chunk_count(self) -> int:
        return len(self._chunks)

    @property
    def vector_enabled(self) -> bool:
        return self._vectors is not None

    def refresh(self) -> None:
        """Reconcile files against the snapshot and rebuild rankings."""
        self.reused_files = 0
        self.created_files = 0
        self.updated_files = 0
        self.removed_files = 0
        self.vector_error = None

        snapshot = load_snapshot(self._index_path) if self._index_path else None
        previous = snapshot.files if snapshot is not None else {}
        self._vector_model = snapshot.embedding_model if snapshot is not None else None
        reuse_vectors = (
            self._embedder is not None
            and snapshot is not None
            and snapshot.embedding_model == self._embedder.model
        )

        current: dict[str, _FileState] = {}
        pending: list[_FileState] = []
        total_chunks = 0
        for path, relative in iter_source_files(self._root):
            if total_chunks >= MAX_CHUNKS:
                break
            signature = _signature(path)
            if signature is None:
                continue
            old = previous.get(relative)
            if old is not None and old.signature == signature:
                stored = [chunk.vector for chunk in old.chunks]
                valid_vectors = bool(stored) and all(
                    vector is not None for vector in stored
                )
                if reuse_vectors and valid_vectors:
                    vectors = stored
                elif self._embedder is None:
                    vectors = stored
                else:
                    vectors = None
                state = _FileState(
                    signature=signature,
                    chunks=[
                        CodeChunk(
                            path=relative,
                            start_line=chunk.start_line,
                            end_line=chunk.end_line,
                            text=chunk.text,
                        )
                        for chunk in old.chunks
                    ],
                    vectors=vectors,
                )
                self.reused_files += 1
            else:
                text = read_text_file(path)
                if text is None:
                    continue
                state = _FileState(
                    signature=signature, chunks=chunk_source(text, relative)
                )
                if old is None:
                    self.created_files += 1
                else:
                    self.updated_files += 1
            current[relative] = state
            total_chunks += len(state.chunks)
            if self._embedder is not None and state.vectors is None and state.chunks:
                pending.append(state)

        self.removed_files = len(set(previous) - set(current))
        self._files = current
        self._embed_pending(pending)
        self._assemble()
        if self._index_path is not None:
            self._save()

    def _embed_pending(self, pending: list[_FileState]) -> None:
        if self._embedder is None or not pending:
            return
        texts = [chunk.text for state in pending for chunk in state.chunks]
        try:
            produced = self._embedder.embed(texts)
        except Exception as error:  # any backend failure degrades to BM25
            self.vector_error = str(error) or "embedding backend failed"
            self._vector_model = None
            _clear_vectors(list(self._files.values()))
            _clear_vectors(pending)
            return
        if len(produced) != len(texts) or any(not vector for vector in produced):
            self.vector_error = "embedding backend returned invalid vectors"
            self._vector_model = None
            _clear_vectors(list(self._files.values()))
            _clear_vectors(pending)
            return
        cursor = 0
        for state in pending:
            size = len(state.chunks)
            state.vectors = produced[cursor : cursor + size]
            cursor += size
        self._vector_model = self._embedder.model

    def _assemble(self) -> None:
        chunks: list[CodeChunk] = []
        vectors: list[list[float] | None] = []
        for relative in sorted(self._files):
            state = self._files[relative]
            for index, chunk in enumerate(state.chunks):
                chunks.append(chunk)
                if state.vectors is None:
                    vectors.append(None)
                else:
                    vectors.append(state.vectors[index])

        dimensions = {len(vector) for vector in vectors if vector}
        if len(dimensions) > 1:
            self.vector_error = "embedding dimensions are inconsistent"
            self._vector_model = None
            _clear_vectors(list(self._files.values()))
            vectors = [None] * len(chunks)

        self._chunks = chunks
        self._vectors = (
            vectors if self._embedder is not None and chunks and any(vectors) else None
        )
        if (
            self._vectors is None
            and chunks
            and not self.vector_error
            and self._embedder is not None
        ):
            self.vector_error = "no embedding vectors were produced"
        self._bm25 = Bm25Index(chunks, file_count=len(self._files))

    def _save(self) -> None:
        if self._index_path is None:
            return
        snapshot = IndexSnapshot(
            embedding_model=self._vector_model,
            files={
                relative: StoredFile(
                    signature=state.signature,
                    chunks=_stored_chunks(state),
                )
                for relative, state in self._files.items()
            },
        )
        try:
            save_snapshot(self._index_path, snapshot)
        except OSError:
            # Persistence is best-effort; an unwritable cache must not break search.
            return

    def _vector_scores(self, query: str) -> list[float | None] | None:
        if self._vectors is None or self._embedder is None:
            return None
        try:
            query_vector = self._embedder.embed([query])[0]
        except Exception as error:  # degrade this search and later ones to BM25
            self.vector_error = str(error) or "embedding backend failed"
            self._vectors = None
            return None
        if not query_vector:
            return None
        return [
            max(0.0, cosine(query_vector, vector)) if vector else None
            for vector in self._vectors
        ]

    def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        """Return the best chunks for ``query`` using lexical and vector ranks."""
        if limit <= 0 or not self._chunks:
            return []
        lexical = self._bm25.scores(query)
        raw_vectors = self._vector_scores(query)
        if raw_vectors is None:
            fused = _fuse(lexical, None, self._alpha)
        else:
            filled = [score or 0.0 for score in raw_vectors]
            fused = _fuse(lexical, filled, self._alpha)

        ranked = sorted(
            ((score, index) for index, score in enumerate(fused) if score > 0),
            key=lambda item: (-item[0], self._chunks[item[1]].path, item[1]),
        )[:limit]
        hits: list[SearchHit] = []
        for score, index in ranked:
            chunk = self._chunks[index]
            vector_score = raw_vectors[index] if raw_vectors is not None else None
            hits.append(
                SearchHit(
                    path=chunk.path,
                    start_line=chunk.start_line,
                    end_line=chunk.end_line,
                    score=round(score, 6),
                    snippet=_snippet(chunk.text),
                    lexical_score=round(lexical[index], 6),
                    vector_score=(
                        round(vector_score, 6) if vector_score is not None else None
                    ),
                )
            )
        return hits


def _snippet(text: str, limit: int = 800) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n..."
