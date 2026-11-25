import asyncio
import subprocess
from pathlib import Path

import pytest

from bale_harness.agent.checkpoint import ShadowGit, ShadowGitError, Snapshot


def _run(coro):
    return asyncio.run(coro)


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def test_init_creates_baseline_and_diff_detects_changes(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"one\n")
    shadow = ShadowGit(tmp_path)
    try:
        baseline = _run(shadow.init())
        assert len(baseline) == 40

        (tmp_path / "a.txt").write_bytes(b"two\n")
        (tmp_path / "b.txt").write_bytes(b"brand new\n")

        diff = _run(shadow.diff())
        assert "-one" in diff
        assert "+two" in diff
        assert "b.txt" in diff
    finally:
        _run(shadow.cleanup())


def test_snapshot_returns_none_without_changes(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"one\n")
    shadow = ShadowGit(tmp_path)
    try:
        _run(shadow.init())

        assert _run(shadow.snapshot("noop")) is None

        (tmp_path / "a.txt").write_bytes(b"two\n")
        snap = _run(shadow.snapshot("change"))
        assert snap is not None
        assert len(snap) == 40
    finally:
        _run(shadow.cleanup())


def test_restore_reverts_changes_and_removes_new_files(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"one\n")
    shadow = ShadowGit(tmp_path)
    try:
        _run(shadow.init())
        (tmp_path / "a.txt").write_bytes(b"two\n")
        (tmp_path / "b.txt").write_bytes(b"brand new\n")

        _run(shadow.restore())

        assert (tmp_path / "a.txt").read_bytes() == b"one\n"
        assert not (tmp_path / "b.txt").exists()
    finally:
        _run(shadow.cleanup())


def test_user_git_repository_is_untouched(tmp_path: Path) -> None:
    _git(tmp_path, "init", "--quiet")
    _git(tmp_path, "config", "user.name", "User")
    _git(tmp_path, "config", "user.email", "user@example.com")
    (tmp_path / "a.txt").write_bytes(b"one\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "--quiet", "-m", "user commit")

    head_before = _git(tmp_path, "rev-parse", "HEAD")
    status_before = _git(tmp_path, "status", "--porcelain")

    shadow = ShadowGit(tmp_path)
    try:
        _run(shadow.init())

        # Merely creating the shadow repository must not touch the user repo.
        assert _git(tmp_path, "rev-parse", "HEAD") == head_before
        assert _git(tmp_path, "status", "--porcelain") == status_before

        (tmp_path / "a.txt").write_bytes(b"two\n")
        _run(shadow.snapshot("change"))

        # The working tree changed, but the user's history did not.
        assert _git(tmp_path, "rev-parse", "HEAD") == head_before
        assert _git(tmp_path, "rev-list", "--count", "HEAD") == "1"
    finally:
        _run(shadow.cleanup())


def test_git_dir_inside_workspace_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ShadowGitError, match="outside the workspace"):
        ShadowGit(tmp_path, git_dir=tmp_path / "shadow")


def test_snapshot_before_init_raises(tmp_path: Path) -> None:
    with pytest.raises(ShadowGitError, match="not been initialized"):
        _run(ShadowGit(tmp_path).snapshot("early"))


def test_reused_git_dir_keeps_original_baseline(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.txt").write_bytes(b"one\n")
    git_dir = tmp_path / "shadow"

    async def first_run() -> str:
        shadow = ShadowGit(workspace, git_dir)
        return await shadow.init()

    baseline = _run(first_run())
    (workspace / "a.txt").write_bytes(b"two\n")

    async def second_run() -> tuple[str, str]:
        shadow = ShadowGit(workspace, git_dir)
        reused = await shadow.init()
        return reused, await shadow.diff()

    reused, diff = _run(second_run())

    assert reused == baseline
    assert "-one" in diff
    assert "+two" in diff


def test_list_snapshots_returns_newest_first(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.txt").write_bytes(b"one\n")
    git_dir = tmp_path / "shadow"

    async def scenario() -> list[Snapshot]:
        shadow = ShadowGit(workspace, git_dir)
        await shadow.init()
        (workspace / "a.txt").write_bytes(b"two\n")
        await shadow.snapshot("first change")
        (workspace / "a.txt").write_bytes(b"three\n")
        await shadow.snapshot("second change")
        return await ShadowGit(workspace, git_dir).list_snapshots()

    snapshots = _run(scenario())

    assert [item.message for item in snapshots] == [
        "second change",
        "first change",
        "bale: baseline",
    ]
    assert all(len(item.commit) == 40 for item in snapshots)
    assert all(item.created_at for item in snapshots)


def test_list_snapshots_without_checkpoint_is_empty(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    shadow = ShadowGit(workspace, tmp_path / "missing-shadow")
    assert _run(shadow.list_snapshots()) == []


def test_remove_deletes_checkpoint_directory(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.txt").write_bytes(b"one\n")
    git_dir = tmp_path / "shadow"

    async def scenario() -> None:
        shadow = ShadowGit(workspace, git_dir)
        await shadow.init()
        await shadow.remove()

    _run(scenario())

    assert not git_dir.exists()
    assert (workspace / "a.txt").read_bytes() == b"one\n"
