import asyncio
from pathlib import Path

from bale_harness.agent.loop import build_default_loop
from bale_harness.agent.messages import ModelResponse
from bale_harness.contracts import Turn
from bale_harness.providers import FakeProvider
from bale_harness.repo_index import Bm25Retriever


class _BrokenRetriever:
    def retrieve(self, query: str) -> list[str]:
        raise RuntimeError("index unavailable")


def _turn(tmp_path: Path, message: str) -> Turn:
    return Turn(
        message=message,
        workspace=tmp_path,
        token_budget=10_000,
        max_iterations=2,
    )


def test_retriever_ranks_relevant_chunk_first(tmp_path: Path) -> None:
    (tmp_path / "widgets.py").write_text(
        "def engage_hyperdrive():\n    return 'go'\n", encoding="utf-8"
    )
    (tmp_path / "docs.md").write_text("notes about lunch\n", encoding="utf-8")

    excerpts = Bm25Retriever(tmp_path, top_k=3).retrieve("how do I engage hyperdrive?")

    assert excerpts
    assert excerpts[0].startswith("widgets.py:1-2")
    assert "engage_hyperdrive" in excerpts[0]


def test_retriever_caps_hits_and_total_chars(tmp_path: Path) -> None:
    for number in range(5):
        (tmp_path / f"mod{number}.py").write_text(
            f"def target_{number}():\n    return {number}\n",
            encoding="utf-8",
        )

    assert len(Bm25Retriever(tmp_path, top_k=2).retrieve("target")) == 2
    assert len(Bm25Retriever(tmp_path, top_k=5, max_chars=10).retrieve("target")) == 1


def test_retriever_returns_nothing_for_unknown_terms(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")

    assert Bm25Retriever(tmp_path).retrieve("zzz_missing") == []


def test_retriever_rejects_invalid_limits(tmp_path: Path) -> None:
    try:
        Bm25Retriever(tmp_path, top_k=0)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for top_k=0")

    try:
        Bm25Retriever(tmp_path, max_chars=0)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for max_chars=0")


def test_loop_injects_retrieved_excerpts_into_system_prompt(tmp_path: Path) -> None:
    (tmp_path / "engine.py").write_text(
        "def engage_hyperdrive():\n    return True\n", encoding="utf-8"
    )
    provider = FakeProvider([ModelResponse(content="done")])
    loop = build_default_loop(
        provider,
        tmp_path,
        retriever=Bm25Retriever(tmp_path, top_k=2),
    )

    result = asyncio.run(loop.run(_turn(tmp_path, "explain engage hyperdrive")))

    assert result.status == "completed"
    system = provider.calls[0][0][0]
    assert system.role == "system"
    assert "engine.py:1-2" in system.content
    assert "def engage_hyperdrive" in system.content


def test_loop_ignores_retriever_failures(tmp_path: Path) -> None:
    provider = FakeProvider([ModelResponse(content="done")])
    loop = build_default_loop(provider, tmp_path, retriever=_BrokenRetriever())

    result = asyncio.run(loop.run(_turn(tmp_path, "anything")))

    assert result.status == "completed"
    assert result.answer == "done"
