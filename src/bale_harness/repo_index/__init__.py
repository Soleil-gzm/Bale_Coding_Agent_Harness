"""Dependency-free code retrieval with optional hybrid vector ranking."""

from bale_harness.repo_index.embedding import (
    Embedder,
    EmbeddingError,
    OpenAIEmbedder,
    cosine,
    create_embedder,
)
from bale_harness.repo_index.hybrid import HybridIndex
from bale_harness.repo_index.index import (
    Bm25Index,
    CodeChunk,
    SearchHit,
    chunk_lines,
    chunk_source,
    iter_source_files,
    read_text_file,
)
from bale_harness.repo_index.retriever import Bm25Retriever, create_retriever
from bale_harness.repo_index.tokenizer import split_identifier, tokenize

__all__ = [
    "Bm25Index",
    "Bm25Retriever",
    "CodeChunk",
    "Embedder",
    "EmbeddingError",
    "HybridIndex",
    "OpenAIEmbedder",
    "SearchHit",
    "chunk_lines",
    "chunk_source",
    "cosine",
    "create_embedder",
    "create_retriever",
    "iter_source_files",
    "read_text_file",
    "split_identifier",
    "tokenize",
]
