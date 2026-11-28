"""Disk snapshots for incremental repository indexes.

A snapshot is a small JSON document keyed by workspace-relative path. Each
file records its ``mtime_ns:size`` signature plus the chunks and optional
embedding vectors that were computed for it, so a later run can reuse
unchanged work instead of re-reading and re-embedding the whole workspace.
"""

import json
from dataclasses import dataclass
from pathlib import Path

INDEX_VERSION = 1
VECTOR_PRECISION = 6


@dataclass(frozen=True)
class StoredChunk:
    """One persisted code chunk with its optional embedding vector."""

    start_line: int
    end_line: int
    text: str
    vector: list[float] | None = None


@dataclass(frozen=True)
class StoredFile:
    """Persisted state for one workspace file."""

    signature: str
    chunks: list[StoredChunk]


@dataclass(frozen=True)
class IndexSnapshot:
    """All persisted state for one workspace index."""

    embedding_model: str | None
    files: dict[str, StoredFile]


def _serialize(snapshot: IndexSnapshot) -> dict[str, object]:
    files: dict[str, object] = {}
    for path, stored in snapshot.files.items():
        files[path] = {
            "signature": stored.signature,
            "chunks": [
                {
                    "start_line": chunk.start_line,
                    "end_line": chunk.end_line,
                    "text": chunk.text,
                    "vector": (
                        [round(value, VECTOR_PRECISION) for value in chunk.vector]
                        if chunk.vector is not None
                        else None
                    ),
                }
                for chunk in stored.chunks
            ],
        }
    return {
        "version": INDEX_VERSION,
        "embedding_model": snapshot.embedding_model,
        "files": files,
    }


def save_snapshot(path: Path, snapshot: IndexSnapshot) -> None:
    """Write ``snapshot`` atomically, creating parent directories as needed."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(
        json.dumps(_serialize(snapshot), ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(target)


def _parse_vector(value: object) -> list[float] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("vector must be a list")
    vector: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int | float):
            raise ValueError("vector values must be numbers")
        vector.append(float(item))
    return vector


def _parse_file(value: object) -> StoredFile:
    if not isinstance(value, dict):
        raise ValueError("file entry must be an object")
    signature = value.get("signature")
    if not isinstance(signature, str):
        raise ValueError("signature must be a string")
    raw_chunks = value.get("chunks")
    if not isinstance(raw_chunks, list):
        raise ValueError("chunks must be a list")
    chunks: list[StoredChunk] = []
    for item in raw_chunks:
        if not isinstance(item, dict):
            raise ValueError("chunk entry must be an object")
        start = item.get("start_line")
        end = item.get("end_line")
        text = item.get("text")
        if not isinstance(start, int) or not isinstance(end, int):
            raise ValueError("chunk line numbers must be integers")
        if not isinstance(text, str):
            raise ValueError("chunk text must be a string")
        chunks.append(
            StoredChunk(
                start_line=start,
                end_line=end,
                text=text,
                vector=_parse_vector(item.get("vector")),
            )
        )
    return StoredFile(signature=signature, chunks=chunks)


def load_snapshot(path: Path) -> IndexSnapshot | None:
    """Read a snapshot, returning ``None`` when it is missing or unusable."""
    target = Path(path)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != INDEX_VERSION:
        return None
    raw_files = payload.get("files")
    if not isinstance(raw_files, dict):
        return None
    embedding_model = payload.get("embedding_model")
    if embedding_model is not None and not isinstance(embedding_model, str):
        return None
    files: dict[str, StoredFile] = {}
    try:
        for relative, value in raw_files.items():
            if not isinstance(relative, str):
                raise ValueError("file path must be a string")
            files[relative] = _parse_file(value)
    except ValueError:
        return None
    return IndexSnapshot(embedding_model=embedding_model, files=files)
