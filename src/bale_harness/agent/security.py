"""Workspace boundary enforcement for file-accessing tools.

All read tools must resolve user-supplied paths through :class:`WorkspaceGuard`
so that ``..`` traversal, absolute paths outside the workspace, and symlinks
that escape the workspace are rejected before any filesystem access.
"""

from pathlib import Path


class WorkspaceError(Exception):
    """Raised when a path cannot be safely resolved inside the workspace."""


class WorkspaceGuard:
    """Resolves paths against a fixed workspace root and rejects escapes."""

    def __init__(self, workspace: Path) -> None:
        # Canonicalize once; symlinks in the workspace root itself are resolved
        # so containment checks compare against a stable absolute base.
        self._root = Path(workspace).resolve()

    @property
    def root(self) -> Path:
        return self._root

    def resolve(self, relative: str | Path) -> Path:
        """Return the canonical absolute path for ``relative`` inside the workspace.

        Raises :class:`WorkspaceError` if the resolved path is not contained in
        the workspace root (including symlink-target escapes).
        """
        candidate = (self._root / Path(relative)).resolve()
        try:
            candidate.relative_to(self._root)
        except ValueError as error:
            raise WorkspaceError(f"path escapes workspace: {relative}") from error
        return candidate
