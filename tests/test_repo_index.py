import asyncio
import json
from pathlib import Path

from typer.testing import CliRunner

from bale_harness.agent.security import WorkspaceGuard
from bale_harness.agent.tools.code_search import SearchCodeTool
from bale_harness.cli import app
from bale_harness.repo_index import (
    Bm25Index,
    chunk_lines,
    chunk_source,
    iter_source_files,
    tokenize,
)

runner = CliRunner()


def _run(coro):
    return asyncio.run(coro)


def test_tokenize_splits_snake_and_camel_case() -> None:
    assert tokenize("load_config") == ["load", "config"]
    assert tokenize("parseHTTPResponse") == ["parse", "http", "response"]
    assert tokenize("parse_http_response") == ["parse", "http", "response"]


def test_index_ranks_matching_chunk_first(tmp_path: Path) -> None:
    (tmp_path / "alpha.py").write_text(
        "def calculate_total(items):\n    return sum(items)\n", encoding="utf-8"
    )
    (tmp_path / "beta.py").write_text(
        "Documentation about deployments and releases.\n", encoding="utf-8"
    )
    index = Bm25Index.from_directory(tmp_path)

    hits = index.search("calculate total")

    assert hits
    assert hits[0].path == "alpha.py"
    assert hits[0].start_line == 1
    assert "def calculate_total" in hits[0].snippet
    assert index.file_count == 2
    assert index.chunk_count == 2


def test_index_matches_camel_case_identifiers(tmp_path: Path) -> None:
    (tmp_path / "server.py").write_text(
        "def parseHTTPResponse(payload):\n    return payload\n", encoding="utf-8"
    )
    index = Bm25Index.from_directory(tmp_path)

    hits = index.search("http response")

    assert hits
    assert hits[0].path == "server.py"


def test_chunk_lines_uses_windows_with_overlap() -> None:
    text = "\n".join(f"line {number}" for number in range(1, 101))

    chunks = chunk_lines(text, "big.py")

    assert chunks[0].start_line == 1
    assert chunks[0].end_line == 40
    assert chunks[1].start_line == 33
    assert chunks[-1].end_line == 100


def test_chunk_source_splits_python_definitions_and_decorators() -> None:
    text = (
        '"""Module doc."""\n'
        "import os\n"
        "\n"
        "@decorator\n"
        "def first(value):\n"
        "    return value\n"
        "\n"
        "class Second:\n"
        "    def method(self):\n"
        "        return 1\n"
    )

    chunks = chunk_source(text, "example.py")

    assert [(chunk.start_line, chunk.end_line) for chunk in chunks] == [
        (1, 2),
        (4, 6),
        (8, 10),
    ]
    assert chunks[1].text.startswith("@decorator")
    assert "class Second" in chunks[2].text


def test_chunk_source_keeps_trailing_module_code() -> None:
    text = "def run():\n    return 1\n\nif __name__ == '__main__':\n    run()\n"

    chunks = chunk_source(text, "script.py")

    assert [(chunk.start_line, chunk.end_line) for chunk in chunks] == [(1, 2), (4, 5)]


def test_chunk_source_falls_back_on_syntax_errors() -> None:
    body = [f"line {number}" for number in range(4, 101)]
    text = "\n".join(["def broken(:", "    pass", "", *body])

    chunks = chunk_source(text, "broken.py")

    assert chunks[0].start_line == 1
    assert chunks[0].end_line == 40
    assert chunks[1].start_line == 33


def test_chunk_source_uses_line_windows_for_non_python() -> None:
    text = "\n".join(f"line {number}" for number in range(1, 101))

    chunks = chunk_source(text, "notes.md")

    assert chunks[0].end_line == 40
    assert chunks[1].start_line == 33


def test_index_chunks_python_functions_separately(tmp_path: Path) -> None:
    (tmp_path / "mod.py").write_text(
        "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n",
        encoding="utf-8",
    )

    index = Bm25Index.from_directory(tmp_path)

    assert index.chunk_count == 2
    hits = index.search("beta")
    assert hits[0].start_line == 5
    assert hits[0].end_line == 6


def test_index_skips_hidden_binary_and_unsupported_files(tmp_path: Path) -> None:
    hidden = tmp_path / ".hidden"
    hidden.mkdir()
    (hidden / "secret.py").write_text("unique_token\n", encoding="utf-8")
    (tmp_path / "blob.txt").write_bytes(b"unique_token\x00binary")
    (tmp_path / "image.bin").write_bytes(b"unique_token")
    (tmp_path / "notes.md").write_text("visible unique_token\n", encoding="utf-8")

    index = Bm25Index.from_directory(tmp_path)

    hits = index.search("unique_token")

    assert [hit.path for hit in hits] == ["notes.md"]
    paths = [path for _, path in iter_source_files(tmp_path)]
    assert "notes.md" in paths
    assert "image.bin" not in paths
    assert "secret.py" not in paths


def test_index_returns_no_hits_for_unknown_terms(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("alpha\n", encoding="utf-8")
    index = Bm25Index.from_directory(tmp_path)

    assert index.search("zzz_missing") == []
    assert index.search("   ") == []


def test_search_code_tool_formats_hits(tmp_path: Path) -> None:
    (tmp_path / "alpha.py").write_text(
        "def calculate_total(items):\n    return sum(items)\n", encoding="utf-8"
    )
    tool = SearchCodeTool(WorkspaceGuard(tmp_path))

    result = _run(tool.execute({"query": "calculate total"}))

    assert result.status == "success"
    assert result.content.startswith("alpha.py:1-2 score=")
    assert "def calculate_total" in result.content
    assert result.metadata["hits"] == 1
    assert result.metadata["indexed_files"] == 1


def test_search_code_tool_reports_no_matches(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("alpha\n", encoding="utf-8")
    tool = SearchCodeTool(WorkspaceGuard(tmp_path))

    result = _run(tool.execute({"query": "missing_token"}))

    assert result.status == "success"
    assert "no matches" in result.content


def test_search_code_tool_rejects_bad_arguments(tmp_path: Path) -> None:
    tool = SearchCodeTool(WorkspaceGuard(tmp_path))

    for arguments in (
        {"query": ""},
        {"query": "x", "limit": 0},
        {"query": "x", "limit": 1000},
        {"query": "x", "path": 3},
    ):
        try:
            tool.validate_arguments(arguments)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {arguments}")


def test_search_code_tool_denies_path_escape(tmp_path: Path) -> None:
    tool = SearchCodeTool(WorkspaceGuard(tmp_path))

    result = _run(tool.execute({"query": "x", "path": ".."}))

    assert result.status == "error"
    assert result.error_code == "path_escape"


def test_cli_search_prints_ranked_hits(tmp_path: Path) -> None:
    (tmp_path / "alpha.py").write_text(
        "def calculate_total(items):\n    return sum(items)\n", encoding="utf-8"
    )

    result = runner.invoke(
        app,
        ["search", "--workspace", str(tmp_path), "--query", "calculate total"],
    )

    assert result.exit_code == 0, result.output
    assert "alpha.py:1-2 score=" in result.output


def test_cli_search_json_is_machine_readable(tmp_path: Path) -> None:
    (tmp_path / "alpha.py").write_text(
        "def calculate_total(items):\n    return sum(items)\n", encoding="utf-8"
    )

    result = runner.invoke(
        app,
        [
            "search",
            "--workspace",
            str(tmp_path),
            "--query",
            "calculate",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["indexed_files"] == 1
    assert payload["hits"][0]["path"] == "alpha.py"
    assert payload["hits"][0]["start_line"] == 1


def test_cli_search_rejects_unknown_path(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "search",
            "--workspace",
            str(tmp_path),
            "--query",
            "x",
            "--path",
            "missing",
        ],
    )

    assert result.exit_code != 0
