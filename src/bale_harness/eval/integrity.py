"""File snapshots, diffs, and integrity checks for evaluation.

Pure functions with no subprocess dependency. The integrity check compares the
pristine fixture against the candidate workspace produced by the agent and
flags forbidden changes, oversized patches, and deterministic test-cheating
patterns.

``forbidden_changes`` path matches are the exact hard gate; the regex-based
cheating scan (skip/xfail markers, commented-out asserts) is a secondary signal
that helps attribute failures but is not the sole arbiter of cheating.
"""

from __future__ import annotations

import difflib
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from bale_harness.eval.task import IntegritySpec

_IGNORE_DIRS = frozenset({".git", ".pytest_cache", "__pycache__", ".ruff_cache"})
_TEST_GLOBS = ("tests/**", "test_*.py", "*_test.py")
_SKIP_PATTERN = re.compile(r"@pytest\.mark\.(skip|xfail)")
_COMMENTED_ASSERT = re.compile(r"^\s*#\s*assert\b")


@dataclass(frozen=True)
class FileEntry:
    """A single file's identity within a snapshot."""

    path: str
    sha256: str
    size: int


@dataclass
class DiffResult:
    """Paths and line counts that changed between two snapshots."""

    added: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    lines_added: int = 0
    lines_removed: int = 0

    @property
    def files_changed(self) -> int:
        return len(self.added) + len(self.modified) + len(self.deleted)


@dataclass
class Violation:
    """One integrity violation found in the candidate workspace."""

    code: str
    path: str
    detail: str


@dataclass
class IntegrityResult:
    """Outcome of an integrity check, including the underlying diff."""

    violations: list[Violation] = field(default_factory=list)
    diff: DiffResult = field(default_factory=DiffResult)

    @property
    def ok(self) -> bool:
        return not self.violations


def _iter_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(root).parts
        if any(part in _IGNORE_DIRS for part in rel_parts):
            continue
        files.append(path)
    return files


def _rel_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def snapshot(root: Path) -> dict[str, FileEntry]:
    """Snapshot every file under ``root`` as a path -> metadata mapping."""
    root = Path(root)
    result: dict[str, FileEntry] = {}
    for path in _iter_files(root):
        data = path.read_bytes()
        rel = _rel_posix(path, root)
        result[rel] = FileEntry(
            path=rel, sha256=hashlib.sha256(data).hexdigest(), size=len(data)
        )
    return result


def _read_lines(path: Path | None) -> list[str]:
    if path is None or not path.exists():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def _line_changes(before: Path | None, after: Path | None) -> tuple[int, int]:
    before_lines = _read_lines(before)
    after_lines = _read_lines(after)
    added = removed = 0
    for line in difflib.unified_diff(before_lines, after_lines, lineterm=""):
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return added, removed


def _is_test_path(rel_path: str) -> bool:
    posix = PurePosixPath(rel_path)
    return any(posix.match(pattern) for pattern in _TEST_GLOBS)


def _matches_forbidden(rel_path: str, patterns: list[str]) -> bool:
    posix = PurePosixPath(rel_path)
    return any(posix.match(pattern) for pattern in patterns)


def _detect_cheating(
    pristine_root: Path, candidate_root: Path, rel_path: str
) -> list[Violation]:
    """Flag skip/xfail markers or commented asserts introduced into a test file."""
    before_lines = set(_read_lines(pristine_root / rel_path))
    after_lines = _read_lines(candidate_root / rel_path)
    added_lines = [line for line in after_lines if line not in before_lines]
    violations: list[Violation] = []
    for line in added_lines:
        if _SKIP_PATTERN.search(line):
            violations.append(
                Violation(
                    code="cheating",
                    path=rel_path,
                    detail="added pytest skip/xfail marker",
                )
            )
    for line in after_lines:
        if _COMMENTED_ASSERT.match(line):
            violations.append(
                Violation(
                    code="cheating",
                    path=rel_path,
                    detail="commented-out assert line",
                )
            )
    return violations


def check_integrity(
    pristine_root: Path, candidate_root: Path, spec: IntegritySpec
) -> IntegrityResult:
    """Compare pristine and candidate workspaces and return integrity violations."""
    pristine_root = Path(pristine_root)
    candidate_root = Path(candidate_root)
    before = snapshot(pristine_root)
    after = snapshot(candidate_root)

    added = sorted(set(after) - set(before))
    deleted = sorted(set(before) - set(after))
    modified = sorted(
        p for p in set(before) & set(after) if before[p].sha256 != after[p].sha256
    )

    lines_added = lines_removed = 0
    for rel in added:
        a, r = _line_changes(None, candidate_root / rel)
        lines_added += a
        lines_removed += r
    for rel in modified:
        a, r = _line_changes(pristine_root / rel, candidate_root / rel)
        lines_added += a
        lines_removed += r
    for rel in deleted:
        a, r = _line_changes(pristine_root / rel, None)
        lines_added += a
        lines_removed += r

    diff = DiffResult(
        added=added,
        modified=modified,
        deleted=deleted,
        lines_added=lines_added,
        lines_removed=lines_removed,
    )
    violations: list[Violation] = []

    changed = added + modified + deleted
    for rel in changed:
        if _matches_forbidden(rel, spec.forbidden_changes):
            violations.append(
                Violation(
                    code="forbidden_change",
                    path=rel,
                    detail="matches forbidden_changes",
                )
            )

    if spec.read_only:
        for rel in changed:
            violations.append(
                Violation(
                    code="read_only",
                    path=rel,
                    detail="read-only task: file changed",
                )
            )

    files_changed = len(changed)
    if spec.max_files_changed is not None and files_changed > spec.max_files_changed:
        limit = spec.max_files_changed
        violations.append(
            Violation(
                code="max_files",
                path="",
                detail=f"{files_changed} files changed exceeds limit {limit}",
            )
        )
    total_lines = lines_added + lines_removed
    if spec.max_lines_changed is not None and total_lines > spec.max_lines_changed:
        limit = spec.max_lines_changed
        violations.append(
            Violation(
                code="max_lines",
                path="",
                detail=f"{total_lines} lines changed exceeds limit {limit}",
            )
        )

    for rel in modified + added:
        if _is_test_path(rel):
            violations.extend(_detect_cheating(pristine_root, candidate_root, rel))

    return IntegrityResult(violations=violations, diff=diff)
