"""In-memory BM25 index over code chunks.

The index is intentionally dependency-free: no embedding model, vector store,
or third-party ranker is required. Files are read once, split into chunks,
tokenized, and scored with BM25 (``k1=1.5``, ``b=0.75``).

Python files are chunked along top-level definitions (decorators included) so a
hit usually points at one function or class; other files, and Python files with
syntax errors, fall back to overlapping line windows.

Scores only express relative lexical relevance for one query; they are not
answer confidences. Hidden directories, common build/cache directories, binary
files, unsupported extensions, and oversized files are skipped.
"""

import ast
import math
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from bale_harness.repo_index.tokenizer import tokenize

CHUNK_LINES = 40
CHUNK_OVERLAP = 8
MAX_FILE_BYTES = 256 * 1024
MAX_FILES = 2000
MAX_CHUNKS = 10_000
DEFAULT_K1 = 1.5
DEFAULT_B = 0.75
SNIPPET_CHARS = 800

SKIP_DIRS = frozenset(
    {
        "__pycache__",
        "node_modules",
        "venv",
        "dist",
        "build",
        "output",
        "site-packages",
        "target",
        "vendor",
        "coverage",
    }
)

TEXT_SUFFIXES = frozenset(
    {
        ".py",
        ".pyi",
        ".pyx",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".java",
        ".kt",
        ".kts",
        ".go",
        ".rs",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".hpp",
        ".cs",
        ".rb",
        ".php",
        ".swift",
        ".scala",
        ".sh",
        ".bash",
        ".zsh",
        ".ps1",
        ".sql",
        ".md",
        ".rst",
        ".txt",
        ".toml",
        ".yaml",
        ".yml",
        ".json",
        ".ini",
        ".cfg",
        ".html",
        ".css",
        ".scss",
        ".vue",
        ".svelte",
        ".lua",
        ".r",
        ".pl",
        ".ex",
        ".exs",
        ".dart",
        ".gradle",
        ".properties",
        ".xml",
    }
)


@dataclass(frozen=True)
class CodeChunk:
    """A contiguous window of lines from one indexed file."""

    path: str
    start_line: int
    end_line: int
    text: str


@dataclass(frozen=True)
class SearchHit:
    """One ranked code chunk returned by an index search.

    ``score`` is the ranking value used by the index that produced the hit.
    ``lexical_score`` and ``vector_score`` are populated only by hybrid
    indexes so callers can explain why a chunk ranked where it did.
    """

    path: str
    start_line: int
    end_line: int
    score: float
    snippet: str
    lexical_score: float | None = None
    vector_score: float | None = None


def _relative_parts(path: Path, root: Path) -> tuple[str, ...] | None:
    try:
        return path.relative_to(root).parts
    except ValueError:
        return None


def iter_source_files(root: Path) -> Iterator[tuple[Path, str]]:
    """Yield ``(absolute_path, posix_relative_path)`` for candidate text files."""
    root = Path(root)
    count = 0
    for candidate in sorted(root.rglob("*")):
        if count >= MAX_FILES:
            return
        if not candidate.is_file():
            continue
        parts = _relative_parts(candidate, root)
        if parts is None or not parts:
            continue
        if any(part.startswith(".") or part in SKIP_DIRS for part in parts[:-1]):
            continue
        if parts[-1].startswith("."):
            continue
        if candidate.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            if candidate.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        count += 1
        yield candidate, "/".join(parts)


def read_text_file(path: Path) -> str | None:
    """Read a UTF-8 text file, returning ``None`` for binary or unreadable files."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def chunk_lines(text: str, path: str) -> list[CodeChunk]:
    """Split ``text`` into overlapping line windows with 1-based line numbers."""
    lines = text.splitlines()
    if not lines:
        return []
    window = max(1, CHUNK_LINES - CHUNK_OVERLAP)
    chunks: list[CodeChunk] = []
    start = 0
    while start < len(lines):
        end = min(start + CHUNK_LINES, len(lines))
        chunks.append(
            CodeChunk(
                path=path,
                start_line=start + 1,
                end_line=end,
                text="\n".join(lines[start:end]),
            )
        )
        if end >= len(lines):
            break
        start += window
    return chunks


def _snippet(text: str) -> str:
    if len(text) <= SNIPPET_CHARS:
        return text
    return text[:SNIPPET_CHARS].rstrip() + "\n..."


PYTHON_SUFFIXES = frozenset({".py", ".pyi"})


def _definition_ranges(tree: ast.Module) -> list[tuple[int, int]]:
    """Return inclusive 1-based line ranges for top-level defs and classes."""
    ranges: list[tuple[int, int]] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        start = min(
            [node.lineno, *(decorator.lineno for decorator in node.decorator_list)]
        )
        ranges.append((start, node.end_lineno or node.lineno))
    return ranges


def _chunk_python(text: str, path: str) -> list[CodeChunk]:
    tree = ast.parse(text)
    lines = text.splitlines()
    chunks: list[CodeChunk] = []
    cursor = 1
    for start, end in _definition_ranges(tree):
        if start > cursor:
            chunks.append(_make_chunk(lines, cursor, start - 1, path))
        chunks.append(_make_chunk(lines, start, end, path))
        cursor = max(cursor, end + 1)
    if cursor <= len(lines):
        chunks.append(_make_chunk(lines, cursor, len(lines), path))
    return [chunk for chunk in chunks if chunk is not None]


def _make_chunk(
    lines: list[str], start_line: int, end_line: int, path: str
) -> CodeChunk | None:
    start = start_line
    end = end_line
    while start <= end and not lines[start - 1].strip():
        start += 1
    while end >= start and not lines[end - 1].strip():
        end -= 1
    if start > end:
        return None
    return CodeChunk(
        path=path,
        start_line=start,
        end_line=end,
        text="\n".join(lines[start - 1 : end]),
    )


def chunk_source(text: str, path: str) -> list[CodeChunk]:
    """Split source text into chunks, preferring Python definition boundaries.

    Files that are not Python, or that fail to parse, fall back to
    :func:`chunk_lines`.
    """
    if Path(path).suffix.lower() not in PYTHON_SUFFIXES:
        return chunk_lines(text, path)
    try:
        return _chunk_python(text, path)
    except (SyntaxError, ValueError):
        return chunk_lines(text, path)


class Bm25Index:
    """An immutable BM25 index over a fixed list of code chunks."""

    def __init__(
        self,
        chunks: list[CodeChunk],
        *,
        file_count: int = 0,
        k1: float = DEFAULT_K1,
        b: float = DEFAULT_B,
    ) -> None:
        self._chunks = list(chunks)
        self._file_count = file_count
        self._k1 = k1
        self._b = b
        self._term_freqs = [Counter(tokenize(chunk.text)) for chunk in chunks]
        self._lengths = [sum(counts.values()) for counts in self._term_freqs]
        total_terms = sum(self._lengths)
        self._avg_length = total_terms / len(chunks) if chunks else 0.0
        doc_freq: Counter[str] = Counter()
        for counts in self._term_freqs:
            doc_freq.update(counts.keys())
        total = len(chunks)
        self._idf = {
            term: math.log(1 + (total - freq + 0.5) / (freq + 0.5))
            for term, freq in doc_freq.items()
        }

    @classmethod
    def from_directory(cls, root: Path) -> "Bm25Index":
        """Build an index by scanning ``root`` with the default file filters."""
        chunks: list[CodeChunk] = []
        file_count = 0
        for file_path, relative in iter_source_files(Path(root)):
            text = read_text_file(file_path)
            if text is None:
                continue
            file_count += 1
            chunks.extend(chunk_source(text, relative))
            if len(chunks) >= MAX_CHUNKS:
                break
        return cls(chunks[:MAX_CHUNKS], file_count=file_count)

    @property
    def file_count(self) -> int:
        return self._file_count

    @property
    def chunk_count(self) -> int:
        return len(self._chunks)

    @property
    def vector_enabled(self) -> bool:
        """BM25-only indexes never expose vector scores."""
        return False

    def scores(self, query: str) -> list[float]:
        """Return one BM25 score per chunk, aligned with index order."""
        scores = [0.0] * len(self._chunks)
        terms = sorted(set(tokenize(query)))
        if not terms or not self._chunks or self._avg_length <= 0:
            return scores
        for term in terms:
            idf = self._idf.get(term)
            if idf is None:
                continue
            for index, counts in enumerate(self._term_freqs):
                frequency = counts.get(term)
                if not frequency:
                    continue
                length = self._lengths[index]
                denominator = frequency + self._k1 * (
                    1 - self._b + self._b * length / self._avg_length
                )
                scores[index] += idf * frequency * (self._k1 + 1) / denominator
        return scores

    def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        """Return the highest-scoring chunks for ``query``."""
        if limit <= 0:
            return []
        scores = self.scores(query)
        ranked = sorted(
            ((score, index) for index, score in enumerate(scores) if score > 0),
            key=lambda item: (-item[0], self._chunks[item[1]].path, item[1]),
        )[:limit]
        return [
            SearchHit(
                path=self._chunks[index].path,
                start_line=self._chunks[index].start_line,
                end_line=self._chunks[index].end_line,
                score=round(score, 6),
                snippet=_snippet(self._chunks[index].text),
            )
            for score, index in ranked
        ]
