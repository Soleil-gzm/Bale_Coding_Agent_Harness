from pathlib import Path

import pytest

from bale_harness.agent.security import WorkspaceError, WorkspaceGuard


def test_resolve_nested_path_inside_workspace(tmp_path: Path) -> None:
    guard = WorkspaceGuard(tmp_path)
    nested = tmp_path / "a" / "b.txt"
    nested.parent.mkdir(parents=True)
    nested.write_text("x", encoding="utf-8")

    resolved = guard.resolve("a/b.txt")

    assert resolved == nested.resolve()
    assert resolved.is_file()


def test_resolve_dot_returns_workspace_root(tmp_path: Path) -> None:
    guard = WorkspaceGuard(tmp_path)

    assert guard.resolve(".") == tmp_path.resolve()
    assert guard.resolve("") == tmp_path.resolve()


def test_parent_traversal_is_denied(tmp_path: Path) -> None:
    guard = WorkspaceGuard(tmp_path)

    with pytest.raises(WorkspaceError):
        guard.resolve("../escape.txt")

    with pytest.raises(WorkspaceError):
        guard.resolve("a/../../escape.txt")


def test_absolute_path_outside_workspace_is_denied(tmp_path: Path) -> None:
    guard = WorkspaceGuard(tmp_path)
    outside = tmp_path.parent / "outside_secret.txt"
    outside.write_text("secret", encoding="utf-8")

    with pytest.raises(WorkspaceError):
        guard.resolve(str(outside))


def test_symlink_escape_is_denied(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside_link_target.txt"
    outside.write_text("secret", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation not permitted in this environment")

    guard = WorkspaceGuard(tmp_path)

    with pytest.raises(WorkspaceError):
        guard.resolve("link.txt")


def test_resolved_path_is_always_beneath_root(tmp_path: Path) -> None:
    guard = WorkspaceGuard(tmp_path)

    for rel in ["file.txt", "dir/nested/file.py", ".hidden", "a/./b"]:
        resolved = guard.resolve(rel)
        resolved.relative_to(guard.root)  # raises if not contained
