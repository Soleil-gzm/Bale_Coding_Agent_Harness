"""Shadow git checkpoints.

A :class:`ShadowGit` keeps its object database in a private git directory and
points ``--work-tree`` at the user's workspace. Every command passes
``--git-dir`` explicitly, so the workspace's own ``.git`` is never read or
written. This makes baseline snapshots, diffs, and rollback safe on repositories
the user owns.

Snapshots use git plumbing only; no branch in the user's repository moves.
"""

import asyncio
import os
import shutil
import stat
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict

GIT_CONFIG = (
    "-c",
    "core.autocrlf=false",
    "-c",
    "core.fileMode=false",
    "-c",
    "core.quotepath=false",
    "-c",
    "core.safecrlf=false",
)


class ShadowGitError(Exception):
    """Raised when a shadow git operation cannot be completed."""


class Snapshot(BaseModel):
    """One commit stored in a shadow git checkpoint."""

    model_config = ConfigDict(extra="forbid")

    commit: str
    created_at: str
    message: str


def _rmtree(path: Path) -> None:
    """Remove ``path`` even when git object files are read-only on Windows."""

    def _retry(func, target, _exc) -> None:
        os.chmod(target, stat.S_IWRITE)
        func(target)

    shutil.rmtree(path, onerror=_retry)


class ShadowGit:
    """Snapshot, diff, and restore a workspace through a private git directory."""

    def __init__(
        self,
        workspace: Path,
        git_dir: Path | None = None,
        *,
        timeout_s: float = 120.0,
    ) -> None:
        self._workspace = Path(workspace).resolve()
        self._git_dir = Path(git_dir).resolve() if git_dir is not None else None
        self._owns_git_dir = git_dir is None
        self._timeout_s = timeout_s
        self._baseline: str | None = None
        self._initialized = False

        if self._git_dir is not None:
            self._reject_nested_git_dir(self._git_dir)

    @property
    def workspace(self) -> Path:
        return self._workspace

    @property
    def git_dir(self) -> Path:
        if self._git_dir is None:
            raise ShadowGitError("shadow repository has not been initialized")
        return self._git_dir

    @property
    def baseline(self) -> str | None:
        return self._baseline

    def _reject_nested_git_dir(self, git_dir: Path) -> None:
        try:
            git_dir.relative_to(self._workspace)
        except ValueError:
            return
        raise ShadowGitError(
            f"shadow git dir must live outside the workspace: {git_dir}"
        )

    def _root_args(self) -> list[str]:
        return [
            "--git-dir",
            str(self.git_dir),
            "--work-tree",
            str(self._workspace),
        ]

    async def _spawn(self, args: list[str], extra_env: dict[str, str] | None = None):
        env = None
        if extra_env is not None:
            env = {**os.environ, **extra_env}
        try:
            return await asyncio.create_subprocess_exec(
                "git",
                *args,
                cwd=str(self._workspace),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as error:
            raise ShadowGitError("git executable not found on PATH") from error

    async def _collect(self, process, label: str) -> tuple[int, str, str]:
        try:
            raw_out, raw_err = await asyncio.wait_for(
                process.communicate(), timeout=self._timeout_s
            )
        except TimeoutError as error:
            process.kill()
            await process.wait()
            raise ShadowGitError(
                f"{label} timed out after {self._timeout_s}s"
            ) from error
        stdout = raw_out.decode("utf-8", errors="replace")
        stderr = raw_err.decode("utf-8", errors="replace")
        return process.returncode or 0, stdout, stderr

    async def _git(self, *args: str, check: bool = True) -> tuple[int, str, str]:
        process = await self._spawn([*GIT_CONFIG, *self._root_args(), *args])
        code, stdout, stderr = await self._collect(process, f"git {args[0]}")
        if check and code != 0:
            detail = stderr.strip() or stdout.strip() or f"exit code {code}"
            raise ShadowGitError(f"git {args[0]} failed: {detail}")
        return code, stdout, stderr

    async def _init_repository(self) -> None:
        # ``git init <path>`` would create ``<path>/.git``; setting GIT_DIR
        # initializes the repository directly at the requested directory.
        process = await self._spawn(
            [*GIT_CONFIG, "init", "--quiet"], {"GIT_DIR": str(self.git_dir)}
        )
        code, _, stderr = await self._collect(process, "git init")
        if code != 0:
            raise ShadowGitError(f"git init failed: {stderr.strip()}")

    async def _write_excludes(self) -> None:
        info_dir = self.git_dir / "info"
        info_dir.mkdir(parents=True, exist_ok=True)
        # Never let the shadow tooling ingest either the user's .git or our own
        # metadata directory, regardless of the workspace's .gitignore.
        (info_dir / "exclude").write_bytes(b".git\n.bale/\n")

    async def _commit(self, message: str) -> str:
        await self._git(
            "commit", "--quiet", "--no-gpg-sign", "--allow-empty", "-m", message
        )
        _, stdout, _ = await self._git("rev-parse", "HEAD")
        return stdout.strip()

    async def init(self) -> str:
        """Create the shadow repository and commit the baseline snapshot.

        When ``git_dir`` already contains commits, the existing HEAD becomes
        the baseline instead of committing the current workspace state. This
        keeps the diff complete across process restarts and resumes.
        """
        if self._initialized:
            if self._baseline is None:
                raise ShadowGitError("shadow repository has no baseline")
            return self._baseline

        if self._git_dir is None:
            self._git_dir = Path(tempfile.mkdtemp(prefix="bale-shadow-")).resolve()
            self._reject_nested_git_dir(self._git_dir)
        self._git_dir.mkdir(parents=True, exist_ok=True)

        await self._init_repository()

        await self._write_excludes()
        await self._git("config", "user.name", "Bale")
        await self._git("config", "user.email", "bale@local")
        code, head, _ = await self._git("rev-parse", "--verify", "HEAD", check=False)
        if code == 0 and head.strip():
            self._baseline = head.strip()
            self._initialized = True
            return self._baseline
        await self._git("add", "-A", "--", ".")
        self._baseline = await self._commit("bale: baseline")
        self._initialized = True
        return self._baseline

    async def snapshot(self, message: str = "bale: snapshot") -> str | None:
        """Commit current workspace state; return ``None`` when nothing changed."""
        if not self._initialized:
            raise ShadowGitError("shadow repository has not been initialized")
        _, status, _ = await self._git("status", "--porcelain")
        if not status.strip():
            return None
        await self._git("add", "-A", "--", ".")
        return await self._commit(message)

    async def diff(self) -> str:
        """Return the unified diff of the workspace against the baseline."""
        if self._baseline is None:
            raise ShadowGitError("shadow repository has no baseline")
        await self._git("add", "-A", "--", ".")
        _, stdout, _ = await self._git("diff", "--cached", "--no-color", self._baseline)
        return stdout

    async def restore(self, commit: str | None = None) -> None:
        """Restore the working tree to ``commit`` (default: baseline)."""
        target = commit or self._baseline
        if target is None:
            raise ShadowGitError("shadow repository has no baseline")
        await self._git("checkout", target, "--", ".")
        await self._git("clean", "-fd", "--", ".")

    async def list_snapshots(self) -> list[Snapshot]:
        """Return snapshot commits newest first without touching the workspace.

        An absent or uninitialized checkpoint yields an empty list instead of
        an error, so inspection commands stay read-only and cheap.
        """
        if self._git_dir is None or not (self._git_dir / "HEAD").exists():
            return []
        code, stdout, stderr = await self._git(
            "log", "--format=%H%x09%aI%x09%s", check=False
        )
        if code != 0:
            detail = stderr.strip() or stdout.strip() or f"exit code {code}"
            raise ShadowGitError(f"git log failed: {detail}")
        snapshots: list[Snapshot] = []
        for line in stdout.splitlines():
            if not line.strip():
                continue
            commit, created_at, message = line.split("\t", 2)
            snapshots.append(
                Snapshot(commit=commit, created_at=created_at, message=message)
            )
        return snapshots

    async def remove(self) -> None:
        """Delete the checkpoint directory and every snapshot stored in it."""
        if self._git_dir is None:
            raise ShadowGitError("shadow repository has not been initialized")
        if self._git_dir.exists():
            _rmtree(self._git_dir)
        self._initialized = False
        self._baseline = None

    async def cleanup(self) -> None:
        """Remove the private git directory when this instance created it."""
        if (
            self._initialized
            and self._owns_git_dir
            and self._git_dir is not None
            and self._git_dir.exists()
        ):
            try:
                _rmtree(self._git_dir)
            except OSError:
                pass  # best effort: leftover temp dirs must not fail the run
        self._initialized = False
