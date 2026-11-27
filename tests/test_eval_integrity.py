"""Tests for file snapshots, diffs, and integrity checks (E1 commit 1)."""

from pathlib import Path

from bale_harness.eval import IntegritySpec, check_integrity, snapshot


def _write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_snapshot_is_stable(tmp_path: Path) -> None:
    _write(tmp_path, "a.py", "x = 1\n")

    assert snapshot(tmp_path) == snapshot(tmp_path)
    assert "a.py" in snapshot(tmp_path)


def test_diff_detects_add_modify_delete(tmp_path: Path) -> None:
    pristine = tmp_path / "pristine"
    candidate = tmp_path / "candidate"
    pristine.mkdir()
    candidate.mkdir()
    _write(pristine, "keep.py", "k=1\n")
    _write(pristine, "mod.py", "x=1\n")
    _write(pristine, "gone.py", "g=1\n")
    _write(candidate, "keep.py", "k=1\n")
    _write(candidate, "mod.py", "x=2\n")
    _write(candidate, "new.py", "n=1\n")

    result = check_integrity(pristine, candidate, IntegritySpec())

    assert result.ok
    assert result.diff.added == ["new.py"]
    assert result.diff.modified == ["mod.py"]
    assert result.diff.deleted == ["gone.py"]
    assert result.diff.lines_added >= 1
    assert result.diff.lines_removed >= 1


def test_forbidden_changes_are_flagged(tmp_path: Path) -> None:
    pristine = tmp_path / "pristine"
    candidate = tmp_path / "candidate"
    pristine.mkdir()
    candidate.mkdir()
    _write(pristine, "app.py", "x=1\n")
    _write(pristine, "tests/test_x.py", "def t():\n    assert True\n")
    _write(candidate, "app.py", "x=1\n")
    _write(candidate, "tests/test_x.py", "def t():\n    assert True\n    assert True\n")

    result = check_integrity(
        pristine, candidate, IntegritySpec(forbidden_changes=["tests/**"])
    )

    assert not result.ok
    assert any(
        v.code == "forbidden_change" and v.path == "tests/test_x.py"
        for v in result.violations
    )


def test_max_files_changed_is_enforced(tmp_path: Path) -> None:
    pristine = tmp_path / "pristine"
    candidate = tmp_path / "candidate"
    pristine.mkdir()
    candidate.mkdir()
    for i in range(3):
        _write(candidate, f"f{i}.py", "x=1\n")

    over = check_integrity(pristine, candidate, IntegritySpec(max_files_changed=2))
    assert not over.ok
    assert any(v.code == "max_files" for v in over.violations)

    within = check_integrity(pristine, candidate, IntegritySpec(max_files_changed=3))
    assert within.ok


def test_max_lines_changed_is_enforced(tmp_path: Path) -> None:
    pristine = tmp_path / "pristine"
    candidate = tmp_path / "candidate"
    pristine.mkdir()
    candidate.mkdir()
    _write(candidate, "big.py", "\n".join(f"x{i}=1" for i in range(10)))

    result = check_integrity(pristine, candidate, IntegritySpec(max_lines_changed=5))

    assert not result.ok
    assert any(v.code == "max_lines" for v in result.violations)


def test_skip_marker_is_flagged_as_cheating(tmp_path: Path) -> None:
    pristine = tmp_path / "pristine"
    candidate = tmp_path / "candidate"
    pristine.mkdir()
    candidate.mkdir()
    _write(pristine, "tests/test_x.py", "import pytest\n\ndef t():\n    assert True\n")
    _write(
        candidate,
        "tests/test_x.py",
        "import pytest\n\n@pytest.mark.skip\ndef t():\n    assert True\n",
    )

    result = check_integrity(pristine, candidate, IntegritySpec())

    assert any(v.code == "cheating" for v in result.violations)


def test_commented_assert_is_flagged_as_cheating(tmp_path: Path) -> None:
    pristine = tmp_path / "pristine"
    candidate = tmp_path / "candidate"
    pristine.mkdir()
    candidate.mkdir()
    _write(pristine, "tests/test_x.py", "def t():\n    assert True\n")
    _write(candidate, "tests/test_x.py", "def t():\n    # assert True\n    pass\n")

    result = check_integrity(pristine, candidate, IntegritySpec())

    assert any(
        v.code == "cheating" and "commented-out assert" in v.detail
        for v in result.violations
    )


def test_read_only_flags_any_change(tmp_path: Path) -> None:
    pristine = tmp_path / "pristine"
    candidate = tmp_path / "candidate"
    pristine.mkdir()
    candidate.mkdir()
    _write(pristine, "app.py", "x=1\n")
    _write(candidate, "app.py", "x=1\n")
    _write(candidate, "extra.py", "y=2\n")

    result = check_integrity(pristine, candidate, IntegritySpec(read_only=True))

    assert not result.ok
    assert any(v.code == "read_only" for v in result.violations)


def test_read_only_passes_when_unchanged(tmp_path: Path) -> None:
    pristine = tmp_path / "pristine"
    candidate = tmp_path / "candidate"
    pristine.mkdir()
    candidate.mkdir()
    _write(pristine, "app.py", "x=1\n")
    _write(candidate, "app.py", "x=1\n")

    result = check_integrity(pristine, candidate, IntegritySpec(read_only=True))

    assert result.ok
