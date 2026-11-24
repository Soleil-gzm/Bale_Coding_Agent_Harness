import asyncio
from pathlib import Path
from typing import Any

from bale_harness.agent.security import WorkspaceGuard
from bale_harness.agent.tools.write import EditFileTool, WriteFileTool


def _run(tool: Any, arguments: dict[str, Any]):
    return asyncio.run(tool.execute(arguments))


def _tools(tmp_path: Path) -> tuple[WriteFileTool, EditFileTool]:
    guard = WorkspaceGuard(tmp_path)
    return WriteFileTool(guard), EditFileTool(guard)


def test_write_file_creates_file_and_parents(tmp_path: Path) -> None:
    write, _ = _tools(tmp_path)

    result = _run(write, {"path": "src/pkg/app.py", "content": "print('hi')\n"})

    assert result.status == "success"
    assert result.metadata["created"] is True
    assert (tmp_path / "src/pkg/app.py").read_bytes() == b"print('hi')\n"


def test_write_file_overwrites_existing_file(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"old\n")
    write, _ = _tools(tmp_path)

    result = _run(write, {"path": "a.txt", "content": "new\n"})

    assert result.status == "success"
    assert result.metadata["created"] is False
    assert (tmp_path / "a.txt").read_bytes() == b"new\n"


def test_write_file_rejects_path_escape(tmp_path: Path) -> None:
    write, _ = _tools(tmp_path)

    result = _run(write, {"path": "../outside.txt", "content": "x"})

    assert result.status == "error"
    assert result.error_code == "path_escape"
    assert not (tmp_path.parent / "outside.txt").exists()


def test_write_file_rejects_directory_target(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    write, _ = _tools(tmp_path)

    result = _run(write, {"path": "sub", "content": "x"})

    assert result.status == "error"
    assert result.error_code == "not_a_file"


def test_edit_file_replaces_single_occurrence(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"alpha beta gamma\n")
    _, edit = _tools(tmp_path)

    result = _run(edit, {"path": "a.txt", "old_string": "beta", "new_string": "BETA"})

    assert result.status == "success"
    assert result.metadata["replacements"] == 1
    assert (tmp_path / "a.txt").read_bytes() == b"alpha BETA gamma\n"


def test_edit_file_rejects_ambiguous_match(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"x\nx\n")
    _, edit = _tools(tmp_path)

    result = _run(edit, {"path": "a.txt", "old_string": "x", "new_string": "y"})

    assert result.status == "error"
    assert result.error_code == "precondition_failed"
    assert (tmp_path / "a.txt").read_bytes() == b"x\nx\n"


def test_edit_file_replace_all(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"x\nx\n")
    _, edit = _tools(tmp_path)

    result = _run(
        edit,
        {"path": "a.txt", "old_string": "x", "new_string": "y", "replace_all": True},
    )

    assert result.status == "success"
    assert result.metadata["replacements"] == 2
    assert (tmp_path / "a.txt").read_bytes() == b"y\ny\n"


def test_edit_file_rejects_missing_old_string(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"alpha\n")
    _, edit = _tools(tmp_path)

    result = _run(edit, {"path": "a.txt", "old_string": "zzz", "new_string": "y"})

    assert result.status == "error"
    assert result.error_code == "precondition_failed"


def test_edit_file_preserves_crlf_line_endings(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"one\r\ntwo\r\n")
    _, edit = _tools(tmp_path)

    result = _run(edit, {"path": "a.txt", "old_string": "two", "new_string": "TWO"})

    assert result.status == "success"
    assert (tmp_path / "a.txt").read_bytes() == b"one\r\nTWO\r\n"


def test_edit_file_rejects_missing_file(tmp_path: Path) -> None:
    _, edit = _tools(tmp_path)

    result = _run(edit, {"path": "missing.txt", "old_string": "a", "new_string": "b"})

    assert result.status == "error"
    assert result.error_code == "not_found"
