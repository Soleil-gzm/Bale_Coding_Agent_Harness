import asyncio
import hashlib
import json
import os
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from bale_harness.agent.security import WorkspaceGuard
from bale_harness.agent.tools.code_search import SearchCodeTool
from bale_harness.repo_index import (
    Bm25Index,
    EmbeddingError,
    HybridIndex,
    OpenAIEmbedder,
    create_embedder,
    create_retriever,
    tokenize,
)
from bale_harness.repo_index.retriever import Bm25Retriever
from bale_harness.settings import Settings

DIMENSION = 256


class CountingEmbedder:
    """Deterministic bag-of-canonical-tokens embedder with call accounting."""

    model = "fake-embed"

    def __init__(
        self,
        *,
        synonyms: dict[str, str] | None = None,
        fail: bool = False,
    ) -> None:
        self.calls: list[list[str]] = []
        self._synonyms = synonyms or {}
        self._fail = fail

    def embed(self, texts):
        self.calls.append(list(texts))
        if self._fail:
            raise EmbeddingError("backend unavailable")
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * DIMENSION
            for token in tokenize(text):
                canonical = self._synonyms.get(token, token)
                digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
                vector[int(digest, 16) % DIMENSION] += 1.0
            vectors.append(vector)
        return vectors


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_hybrid_recalls_vector_only_match(tmp_path: Path) -> None:
    _write(
        tmp_path / "engine.py",
        "def engage_hyperdrive():\n    return velocity\n",
    )
    _write(tmp_path / "notes.md", "lunch plans for friday\n")
    embedder = CountingEmbedder(synonyms={"warp": "hyperdrive", "speed": "velocity"})

    index = HybridIndex(tmp_path, embedder=embedder)
    hits = index.search("warp speed")

    assert index.vector_enabled
    assert hits
    assert hits[0].path == "engine.py"
    assert hits[0].lexical_score == 0.0
    assert hits[0].vector_score is not None


def test_hybrid_ranks_lexical_hit_without_embedder(tmp_path: Path) -> None:
    _write(
        tmp_path / "alpha.py", "def calculate_total(items):\n    return sum(items)\n"
    )
    _write(tmp_path / "beta.py", "deployment notes\n")

    hybrid = HybridIndex(tmp_path)
    lexical = Bm25Index.from_directory(tmp_path)

    assert [hit.path for hit in hybrid.search("calculate total")] == [
        hit.path for hit in lexical.search("calculate total")
    ]
    assert hybrid.vector_enabled is False


def test_hybrid_falls_back_to_bm25_when_embedding_fails(tmp_path: Path) -> None:
    _write(
        tmp_path / "alpha.py", "def calculate_total(items):\n    return sum(items)\n"
    )

    index = HybridIndex(tmp_path, embedder=CountingEmbedder(fail=True))

    assert index.vector_enabled is False
    assert "backend unavailable" in (index.vector_error or "")
    hits = index.search("calculate total")
    assert hits[0].path == "alpha.py"
    assert hits[0].vector_score is None


def test_retriever_supports_hybrid_embedder(tmp_path: Path) -> None:
    _write(tmp_path / "engine.py", "def engage_hyperdrive():\n    return velocity\n")
    embedder = CountingEmbedder(synonyms={"warp": "hyperdrive", "speed": "velocity"})

    excerpts = Bm25Retriever(tmp_path, top_k=2, embedder=embedder).retrieve(
        "warp speed"
    )

    assert excerpts
    assert excerpts[0].startswith("engine.py:1-2")


def test_incremental_rebuild_reuses_unchanged_files(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    _write(workspace / "alpha.py", "def alpha():\n    return 1\n")
    _write(workspace / "beta.py", "def beta():\n    return 2\n")
    index_path = tmp_path / "cache" / "index.json"
    first = CountingEmbedder()

    index = HybridIndex(workspace, embedder=first, index_path=index_path)

    assert index.created_files == 2
    assert index.reused_files == 0
    assert first.calls

    second = CountingEmbedder()
    rebuilt = HybridIndex(workspace, embedder=second, index_path=index_path)

    assert rebuilt.reused_files == 2
    assert rebuilt.created_files == 0
    assert second.calls == []
    assert rebuilt.search("alpha")[0].path == "alpha.py"


def test_incremental_rebuild_applies_added_modified_and_deleted_files(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    _write(workspace / "alpha.py", "def alpha():\n    return 1\n")
    _write(workspace / "beta.py", "def beta():\n    return 2\n")
    index_path = tmp_path / "index.json"
    HybridIndex(workspace, index_path=index_path)

    time.sleep(0.01)
    _write(workspace / "alpha.py", "def alpha_updated():\n    return 3\n")
    _write(workspace / "gamma.py", "def gamma():\n    return 4\n")
    os.remove(workspace / "beta.py")

    rebuilt = HybridIndex(workspace, index_path=index_path)

    assert rebuilt.created_files == 1
    assert rebuilt.updated_files == 1
    assert rebuilt.removed_files == 1
    assert rebuilt.file_count == 2
    assert rebuilt.search("alpha")[0].snippet.startswith("def alpha_updated")
    assert rebuilt.search("beta") == []


def test_snapshot_persists_chunks_without_an_embedder(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    _write(workspace / "alpha.py", "def alpha():\n    return 1\n")
    index_path = tmp_path / "index.json"
    HybridIndex(workspace, embedder=CountingEmbedder(), index_path=index_path)

    assert index_path.exists()
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    assert payload["embedding_model"] == "fake-embed"
    assert payload["files"]["alpha.py"]["chunks"][0]["vector"]

    reloaded = HybridIndex(workspace, index_path=index_path)

    assert reloaded.reused_files == 1
    assert reloaded.vector_enabled is False
    assert reloaded.search("alpha")[0].path == "alpha.py"
    assert json.loads(index_path.read_text(encoding="utf-8"))["embedding_model"] == (
        "fake-embed"
    )


def test_snapshot_ignored_when_corrupt(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    _write(workspace / "alpha.py", "def alpha():\n    return 1\n")
    index_path = tmp_path / "index.json"
    index_path.write_text("{not json", encoding="utf-8")

    index = HybridIndex(workspace, index_path=index_path)

    assert index.created_files == 1
    assert index.search("alpha")[0].path == "alpha.py"


def test_openai_embedder_posts_batches_with_auth() -> None:
    captured: dict[str, object] = {}

    def fetch(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data)
        return json.dumps(
            {
                "data": [
                    {"index": 1, "embedding": [0.0, 2.0]},
                    {"index": 0, "embedding": [1.0, 0.0]},
                ]
            }
        ).encode("utf-8")

    embedder = OpenAIEmbedder(
        "text-embed",
        "http://local.test/v1/",
        api_key="secret",
        fetcher=fetch,
    )

    vectors = embedder.embed(["first", "second"])

    assert captured["url"] == "http://local.test/v1/embeddings"
    assert captured["body"] == {"model": "text-embed", "input": ["first", "second"]}
    assert captured["headers"].get("Authorization") == "Bearer secret"
    assert vectors == [[1.0, 0.0], [0.0, 2.0]]


def test_openai_embedder_wraps_backend_failures() -> None:
    def fetch(request, timeout):
        raise OSError("network down")

    embedder = OpenAIEmbedder("text-embed", fetcher=fetch)

    with pytest.raises(EmbeddingError):
        embedder.embed(["value"])


def test_openai_embedder_rejects_malformed_responses() -> None:
    def fetch(request, timeout):
        return b'{"data": [{"index": 0, "embedding": []}]}'

    embedder = OpenAIEmbedder("text-embed", fetcher=fetch)

    with pytest.raises(EmbeddingError):
        embedder.embed(["value"])


def test_create_embedder_requires_model_and_remote_key() -> None:
    assert create_embedder(Settings(_env_file=None)) is None
    assert (
        create_embedder(
            Settings(
                _env_file=None,
                retrieval_hybrid=False,
                embedding_model="text-embed",
                openai_api_key="key",
            )
        )
        is None
    )
    assert (
        create_embedder(
            Settings(
                _env_file=None,
                embedding_model="text-embed",
                openai_base_url="https://api.example.com/v1",
            )
        )
        is None
    )
    local = create_embedder(
        Settings(
            _env_file=None,
            embedding_model="text-embed",
            openai_base_url="http://127.0.0.1:11434/v1",
        )
    )
    assert isinstance(local, OpenAIEmbedder)
    remote = create_embedder(
        Settings(
            _env_file=None,
            embedding_model="text-embed",
            embedding_api_key="embed-key",
            embedding_base_url="https://embed.example.com/v1",
        )
    )
    assert isinstance(remote, OpenAIEmbedder)
    assert remote.model == "text-embed"


def test_search_code_tool_reports_vector_search(tmp_path: Path) -> None:
    _write(tmp_path / "engine.py", "def engage_hyperdrive():\n    return velocity\n")
    embedder = CountingEmbedder(synonyms={"warp": "hyperdrive", "speed": "velocity"})
    tool = SearchCodeTool(WorkspaceGuard(tmp_path), embedder=embedder)

    result = asyncio.run(tool.execute({"query": "warp speed"}))

    assert result.status == "success"
    assert result.metadata["vector_search"] is True
    assert "engine.py" in result.content


def test_create_retriever_respects_disabled_retrieval(tmp_path: Path) -> None:
    assert (
        create_retriever(Settings(_env_file=None, retrieval_enabled=False), tmp_path)
        is None
    )
    retriever = create_retriever(Settings(_env_file=None), tmp_path)
    assert isinstance(retriever, Bm25Retriever)


def test_retrieval_alpha_must_be_a_fraction() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, retrieval_alpha=1.5)
    with pytest.raises(ValueError):
        HybridIndex(Path("."), alpha=1.5)
