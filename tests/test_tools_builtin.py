import asyncio
from pathlib import Path

import pytest

from bale_harness.agent.messages import ToolCall
from bale_harness.agent.security import WorkspaceGuard
from bale_harness.agent.tools.builtin import (
    MAX_READ_BYTES,
    MAX_SEARCH_MATCHES,
    ListFilesTool,
    ReadFileTool,
    SearchFilesTool,
    build_default_tools,
)
from bale_harness.agent.tools.registry import ToolRegistry


def _run(coro):
    return asyncio.run(coro)


def _guard(tmp_path: Path) -> WorkspaceGuard:
    return WorkspaceGuard(tmp_path)


def test_read_file_returns_content(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"hello\nworld")
    tool = ReadFileTool(_guard(tmp_path))

    result = _run(tool.execute({"path": "a.txt"}))

    assert result.status == "success"
    assert result.content == "hello\nworld"
    assert result.metadata["path"] == "a.txt"
    assert result.truncated is False


def test_read_file_missing_is_error(tmp_path: Path) -> None:
    tool = ReadFileTool(_guard(tmp_path))

    result = _run(tool.execute({"path": "missing.txt"}))

    assert result.status == "error"
    assert result.error_code == "not_found"


def test_read_file_directory_is_error(tmp_path: Path) -> None:
    (tmp_path / "dir").mkdir()
    tool = ReadFileTool(_guard(tmp_path))

    result = _run(tool.execute({"path": "dir"}))

    assert result.status == "error"
    assert result.error_code == "not_a_file"


def test_read_file_path_escape_denied(tmp_path: Path) -> None:
    tool = ReadFileTool(_guard(tmp_path))

    result = _run(tool.execute({"path": "../secret.txt"}))

    assert result.status == "error"
    assert result.error_code == "path_escape"


def test_read_file_truncates_large_files(tmp_path: Path) -> None:
    big = tmp_path / "big.txt"
    big.write_text("x" * (MAX_READ_BYTES + 1000), encoding="utf-8")
    tool = ReadFileTool(_guard(tmp_path))

    result = _run(tool.execute({"path": "big.txt"}))

    assert result.truncated is True
    assert len(result.content) <= MAX_READ_BYTES
    assert result.metadata["bytes"] == MAX_READ_BYTES


def test_read_file_never_materializes_the_whole_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    big = tmp_path / "big.txt"
    big.write_text("x" * (MAX_READ_BYTES + 1000), encoding="utf-8")

    def explode(self: Path) -> bytes:
        raise AssertionError("read_file must not call read_bytes")

    monkeypatch.setattr(Path, "read_bytes", explode)

    result = _run(ReadFileTool(_guard(tmp_path)).execute({"path": "big.txt"}))

    assert result.status == "success"
    assert result.truncated is True


def test_list_files_lists_entries_with_dir_marker(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    tool = ListFilesTool(_guard(tmp_path))

    result = _run(tool.execute({"path": "."}))

    assert result.status == "success"
    lines = result.content.splitlines()
    assert "a.py" in lines
    assert "sub/" in lines


def test_list_files_default_path_is_workspace_root(tmp_path: Path) -> None:
    (tmp_path / "root.txt").write_text("", encoding="utf-8")
    tool = ListFilesTool(_guard(tmp_path))

    result = _run(tool.execute({}))

    assert "root.txt" in result.content.splitlines()


def test_list_files_path_escape_denied(tmp_path: Path) -> None:
    tool = ListFilesTool(_guard(tmp_path))

    result = _run(tool.execute({"path": ".."}))

    assert result.status == "error"
    assert result.error_code == "path_escape"


def test_search_files_finds_matches(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("alpha\nbeta\nalpha again\n", encoding="utf-8")
    tool = SearchFilesTool(_guard(tmp_path))

    result = _run(tool.execute({"pattern": "alpha", "path": "."}))

    assert result.status == "success"
    lines = result.content.splitlines()
    assert any(line.startswith("a.py:1:") for line in lines)
    assert any(line.startswith("a.py:3:") for line in lines)
    assert result.metadata["count"] == 2


def test_search_files_invalid_regex_rejected(tmp_path: Path) -> None:
    tool = SearchFilesTool(_guard(tmp_path))

    try:
        tool.validate_arguments({"pattern": "["})
    except ValueError as error:
        assert "invalid regex" in str(error)
    else:
        raise AssertionError("expected ValueError for invalid regex")


def test_search_files_caps_matches(tmp_path: Path) -> None:
    content = "match\n" * (MAX_SEARCH_MATCHES + 50)
    (tmp_path / "many.txt").write_text(content, encoding="utf-8")
    tool = SearchFilesTool(_guard(tmp_path))

    result = _run(tool.execute({"pattern": "match", "path": "."}))

    assert result.truncated is True
    assert result.metadata["count"] == MAX_SEARCH_MATCHES


def test_search_files_path_escape_denied(tmp_path: Path) -> None:
    tool = SearchFilesTool(_guard(tmp_path))

    result = _run(tool.execute({"pattern": "x", "path": ".."}))

    assert result.status == "error"
    assert result.error_code == "path_escape"


def test_search_files_subdirectory_symlink_does_not_escape(
    tmp_path: Path,
) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    outside = tmp_path.parent / f"{tmp_path.name}-outside-secret.txt"
    outside.write_text("outside-secret\n", encoding="utf-8")
    link = sub / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation not permitted in this environment")

    result = _run(
        SearchFilesTool(_guard(tmp_path)).execute(
            {"pattern": "outside-secret", "path": "sub"}
        )
    )

    assert result.status == "success"
    assert result.metadata["count"] == 0
    assert "outside-secret" not in result.content


def test_search_files_rejects_candidates_outside_the_workspace(
    tmp_path: Path,
) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside-root"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside-secret\n", encoding="utf-8")
    tool = SearchFilesTool(_guard(tmp_path))

    assert tool._iter_files(outside) == []


def test_registry_executes_builtin_tool(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hi", encoding="utf-8")
    registry = ToolRegistry()
    for tool in build_default_tools(_guard(tmp_path)):
        registry.register(tool)

    result = _run(
        registry.execute(
            ToolCall(call_id="c1", name="read_file", arguments={"path": "a.txt"})
        )
    )

    assert result.status == "success"
    assert result.call_id == "c1"
    assert result.content == "hi"
