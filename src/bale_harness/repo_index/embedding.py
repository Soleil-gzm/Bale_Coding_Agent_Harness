"""Optional OpenAI-compatible embeddings for hybrid code search.

Embeddings are opt-in: without ``BALE_EMBEDDING_MODEL`` the retriever stays
purely lexical. Remote embedding calls use the standard-library HTTP client so
the runtime keeps its small dependency set. Backend failures raise
:class:`EmbeddingError`; callers degrade to BM25 instead of failing a search.
"""

import json
import math
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from typing import Protocol

from bale_harness.settings import Settings, is_local_endpoint

DEFAULT_TIMEOUT_SECONDS = 30.0
BATCH_SIZE = 64

Fetcher = Callable[[urllib.request.Request, float], bytes]


class EmbeddingError(RuntimeError):
    """Raised when an embedding backend cannot produce usable vectors."""


class Embedder(Protocol):
    """Minimal embedding backend used by the hybrid index."""

    model: str

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one dense vector per input text, in the same order."""


def _default_fetch(request: urllib.request.Request, timeout: float) -> bytes:
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """Return cosine similarity, or ``0.0`` for empty or mismatched vectors."""
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


class OpenAIEmbedder:
    """Embed texts through any OpenAI-compatible ``/embeddings`` endpoint."""

    def __init__(
        self,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        *,
        api_key: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        batch_size: int = BATCH_SIZE,
        fetcher: Fetcher | None = None,
    ) -> None:
        if not model.strip():
            raise ValueError("model must be a non-empty string")
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        self.model = model
        self._url = base_url.rstrip("/") + "/embeddings"
        self._api_key = api_key
        self._timeout = timeout
        self._batch_size = batch_size
        self._fetch = fetcher or _default_fetch

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = list(texts[start : start + self._batch_size])
            vectors.extend(self._request(batch))
        return vectors

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _request(self, texts: list[str]) -> list[list[float]]:
        payload = json.dumps({"model": self.model, "input": texts}).encode("utf-8")
        request = urllib.request.Request(
            self._url,
            data=payload,
            headers=self._headers(),
            method="POST",
        )
        try:
            raw = self._fetch(request, self._timeout)
            body = json.loads(raw.decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise EmbeddingError("embedding request failed") from error

        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list) or len(data) != len(texts):
            raise EmbeddingError("embedding response has unexpected shape")

        try:
            ordered = sorted(data, key=lambda item: item["index"])
        except (KeyError, TypeError) as error:
            raise EmbeddingError("embedding response has unexpected shape") from error

        vectors: list[list[float]] = []
        for item in ordered:
            vector = item.get("embedding") if isinstance(item, dict) else None
            if not isinstance(vector, list) or not vector:
                raise EmbeddingError("embedding response has unexpected shape")
            try:
                vectors.append([float(value) for value in vector])
            except (TypeError, ValueError) as error:
                raise EmbeddingError(
                    "embedding response has non-numeric values"
                ) from error
        return vectors


def create_embedder(settings: Settings) -> OpenAIEmbedder | None:
    """Build the configured embedder, or return ``None`` to stay BM25-only.

    An embedding backend is used only when hybrid retrieval is enabled and
    ``BALE_EMBEDDING_MODEL`` is set. Remote endpoints without a key are
    skipped instead of failing configuration, so searches keep working.
    """
    if not settings.retrieval_hybrid or not settings.embedding_model:
        return None
    base_url = settings.embedding_base_url or settings.openai_base_url
    secret = settings.embedding_api_key or settings.openai_api_key
    api_key = secret.get_secret_value().strip() if secret is not None else ""
    if not api_key and not is_local_endpoint(base_url):
        return None
    return OpenAIEmbedder(
        model=settings.embedding_model,
        base_url=base_url,
        api_key=api_key or None,
    )
