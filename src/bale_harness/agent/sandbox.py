"""Sandbox execution port and a local subprocess adapter.

:class:`LocalSandbox` runs commands directly on the host. Its protections are
layered and best-effort only: no shell interpretation, a command deny-list, an
environment-variable allowlist, a workspace-bound working directory, an
enforced timeout, and output truncation.

It is **not** a security boundary. Untrusted code must run in an isolated
container or virtual machine; see :class:`DockerSandbox` for the container
adapter.
"""

import asyncio
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from bale_harness.agent.security import WorkspaceError, WorkspaceGuard

MAX_OUTPUT_CHARS = 30_000

# Only these host variables are forwarded. Everything else (API keys, tokens,
# proxy credentials) is dropped so child processes cannot read secrets.
ENV_WHITELIST = frozenset(
    {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "HOME",
        "USERPROFILE",
        "LANG",
        "LC_ALL",
        "PYTHONIOENCODING",
        "PYTHONPATH",
        "VIRTUAL_ENV",
        "CONDA_PREFIX",
    }
)

# Blocked executables. This is a denial layer, not a sandbox.
DEFAULT_DENY_COMMANDS = frozenset(
    {
        "rm",
        "rmdir",
        "del",
        "erase",
        "format",
        "shutdown",
        "reboot",
        "curl",
        "wget",
        "ssh",
        "scp",
        "sftp",
        "nc",
        "ncat",
        "netcat",
        "telnet",
        "sudo",
        "su",
        "doas",
        "chmod",
        "chown",
        "mkfs",
        "dd",
        "diskpart",
        "cmd",
        "powershell",
        "pwsh",
    }
)


class SandboxViolation(Exception):
    """Raised when a command or working directory violates the local policy."""


class ExecResult(BaseModel):
    """Outcome of one sandboxed command execution."""

    model_config = ConfigDict(extra="forbid")

    command: list[str]
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration_ms: int = Field(default=0, ge=0)
    truncated: bool = False


class Sandbox(Protocol):
    """Execution contract shared by local and container adapters."""

    @property
    def python_executable(self) -> str:
        """Interpreter to use for Python tooling inside this sandbox."""

    async def run(
        self,
        command: list[str],
        *,
        cwd: str = ".",
        timeout_s: float = 120.0,
        extra_env: dict[str, str] | None = None,
    ) -> ExecResult:
        """Run ``command`` and return a normalized result."""


def _elapsed_ms(start: float) -> int:
    return max(0, int((time.perf_counter() - start) * 1000))


def _executable_key(argv0: str) -> str:
    name = Path(argv0).name.lower()
    return name[:-4] if name.endswith(".exe") else name


def _truncate(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text, False
    return text[:MAX_OUTPUT_CHARS], True


def _kill_process_tree(process: asyncio.subprocess.Process) -> None:
    """Best-effort termination of the process and its children."""
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                check=False,
            )
        except OSError:
            pass
    if process.returncode is None:
        try:
            process.kill()
        except (ProcessLookupError, OSError):
            pass


class LocalSandbox:
    """Run commands on the host with layered, non-security protections."""

    def __init__(
        self,
        guard: WorkspaceGuard,
        *,
        env_allowlist: frozenset[str] = ENV_WHITELIST,
        deny_commands: frozenset[str] = DEFAULT_DENY_COMMANDS,
    ) -> None:
        self._guard = guard
        self._env_allowlist = env_allowlist
        self._deny_commands = deny_commands

    @property
    def python_executable(self) -> str:
        return sys.executable

    @property
    def image_digest(self) -> None:
        """Local execution has no pinned image digest."""
        return None

    def _build_env(self, extra_env: dict[str, str] | None) -> dict[str, str]:
        env = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in self._env_allowlist
        }
        env["BALE_SANDBOX"] = "local"
        if extra_env:
            env.update(extra_env)
        return env

    def _check_command(self, argv: list[str]) -> None:
        if not argv:
            raise SandboxViolation("empty command")
        key = _executable_key(argv[0])
        if key in self._deny_commands:
            raise SandboxViolation(f"command is denied by policy: {key}")

    async def run(
        self,
        command: list[str],
        *,
        cwd: str = ".",
        timeout_s: float = 120.0,
        extra_env: dict[str, str] | None = None,
    ) -> ExecResult:
        start = time.perf_counter()
        argv = [str(part) for part in command]
        self._check_command(argv)

        try:
            workdir = self._guard.resolve(cwd)
        except WorkspaceError as error:
            raise SandboxViolation(str(error)) from error
        if not workdir.is_dir():
            raise SandboxViolation(f"working directory is not a directory: {cwd}")

        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(workdir),
            env=self._build_env(extra_env),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        timed_out = False
        try:
            raw_out, raw_err = await asyncio.wait_for(
                process.communicate(), timeout=timeout_s
            )
        except asyncio.CancelledError:
            # Cancelled runs (time budget, HTTP disconnect) must not leave
            # build/test subprocesses mutating the workspace after the turn.
            _kill_process_tree(process)
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                pass
            raise
        except TimeoutError:
            timed_out = True
            _kill_process_tree(process)
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                pass
            raw_out, raw_err = b"", b""

        stdout, out_truncated = _truncate(raw_out.decode("utf-8", errors="replace"))
        stderr, err_truncated = _truncate(raw_err.decode("utf-8", errors="replace"))
        return ExecResult(
            command=argv,
            exit_code=None if timed_out else process.returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            duration_ms=_elapsed_ms(start),
            truncated=out_truncated or err_truncated,
        )


def parse_docker_inspect(stdout: str) -> str | None:
    """Extract an image digest from ``docker image inspect`` output.

    Accepts the ``{{.Id}}`` format (``sha256:<hex>``) or a repo digest
    (``name@sha256:<hex>``). Returns ``None`` when no digest is present so a
    failed lookup never blocks command execution.
    """
    text = (stdout or "").strip()
    if not text:
        return None
    for line in text.splitlines():
        candidate = line.strip()
        if "@sha256:" in candidate:
            return candidate.split("@", 1)[1]
        if candidate.startswith("sha256:"):
            return candidate
    return None


DEFAULT_DOCKER_IMAGE = "python:3.11-slim"
DOCKER_WORKSPACE = "/workspace"


class DockerSandbox:
    """Run every command in a fresh, hardened Docker container.

    The workspace is bind-mounted at ``/workspace``. Containers start with no
    network, a read-only root filesystem, dropped capabilities, a non-root
    user, and CPU/memory/PID limits. The image defines which tools are
    available, so select one that contains the project's runtime and test
    dependencies.
    """

    def __init__(
        self,
        workspace: Path,
        *,
        image: str = DEFAULT_DOCKER_IMAGE,
        network: bool = False,
        memory: str = "1g",
        cpus: float = 1.0,
        pids_limit: int = 256,
        user: str = "10001:10001",
        docker_bin: str = "docker",
    ) -> None:
        self._guard = WorkspaceGuard(workspace)
        self._image = image
        self._network = network
        self._memory = memory
        self._cpus = cpus
        self._pids_limit = pids_limit
        self._user = user
        self._docker_bin = docker_bin
        self._image_digest: str | None = None
        self._image_digest_loaded: bool = False

    @property
    def python_executable(self) -> str:
        return "python"

    @property
    def image_digest(self) -> str | None:
        """Resolve the image digest once; lookup failures stay ``None``."""
        if not self._image_digest_loaded:
            self._image_digest = self._resolve_image_digest()
            self._image_digest_loaded = True
        return self._image_digest

    def _resolve_image_digest(self) -> str | None:
        try:
            completed = subprocess.run(
                [
                    self._docker_bin,
                    "image",
                    "inspect",
                    "--format",
                    "{{.Id}}",
                    self._image,
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode != 0:
            return None
        return parse_docker_inspect(completed.stdout)

    def _require_docker(self) -> None:
        if shutil.which(self._docker_bin) is not None:
            return
        if Path(self._docker_bin).is_file():
            return
        raise SandboxViolation(
            f"docker executable not found: {self._docker_bin}; "
            "install Docker or switch to the local sandbox"
        )

    def _workdir(self, cwd: str) -> str:
        try:
            resolved = self._guard.resolve(cwd)
        except WorkspaceError as error:
            raise SandboxViolation(str(error)) from error
        if not resolved.is_dir():
            raise SandboxViolation(f"working directory is not a directory: {cwd}")
        relative = resolved.relative_to(self._guard.root)
        if relative == Path("."):
            return DOCKER_WORKSPACE
        return f"{DOCKER_WORKSPACE}/{relative.as_posix()}"

    def build_command(
        self,
        command: list[str],
        *,
        cwd: str = ".",
        extra_env: dict[str, str] | None = None,
        name: str,
    ) -> list[str]:
        """Return the full ``docker run`` argument vector for one command."""
        argv = [str(part) for part in command]
        if not argv:
            raise SandboxViolation("empty command")
        workdir = self._workdir(cwd)
        args = [
            self._docker_bin,
            "run",
            "--rm",
            "--init",
            "--name",
            name,
            "--label",
            "bale.sandbox=1",
            "--network",
            "bridge" if self._network else "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,size=64m",
            "--security-opt",
            "no-new-privileges",
            "--cap-drop",
            "ALL",
            "--pids-limit",
            str(self._pids_limit),
            "--memory",
            self._memory,
            "--cpus",
            f"{self._cpus:g}",
            "--user",
            self._user,
            "-e",
            "HOME=/tmp",
            "-e",
            "BALE_SANDBOX=docker",
            "-e",
            "PYTHONUNBUFFERED=1",
            "-v",
            f"{self._guard.root}:{DOCKER_WORKSPACE}",
            "--workdir",
            workdir,
        ]
        for key, value in sorted((extra_env or {}).items()):
            args.extend(["-e", f"{key}={value}"])
        args.extend([self._image, *argv])
        return args

    async def _remove_container(self, name: str) -> None:
        try:
            process = await asyncio.create_subprocess_exec(
                self._docker_bin,
                "rm",
                "-f",
                name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except TimeoutError:
            process.kill()

    async def run(
        self,
        command: list[str],
        *,
        cwd: str = ".",
        timeout_s: float = 120.0,
        extra_env: dict[str, str] | None = None,
    ) -> ExecResult:
        start = time.perf_counter()
        self._require_docker()
        name = f"bale-sandbox-{uuid4().hex[:12]}"
        argv = [str(part) for part in command]
        docker_argv = self.build_command(argv, cwd=cwd, extra_env=extra_env, name=name)

        try:
            process = await asyncio.create_subprocess_exec(
                *docker_argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as error:
            raise SandboxViolation(
                f"docker executable not found: {self._docker_bin}"
            ) from error

        timed_out = False
        try:
            raw_out, raw_err = await asyncio.wait_for(
                process.communicate(), timeout=timeout_s
            )
        except TimeoutError:
            timed_out = True
            await self._remove_container(name)
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                pass
            raw_out, raw_err = b"", b""
        except asyncio.CancelledError:
            await self._remove_container(name)
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            raise

        stdout, out_truncated = _truncate(raw_out.decode("utf-8", errors="replace"))
        stderr, err_truncated = _truncate(raw_err.decode("utf-8", errors="replace"))
        return ExecResult(
            command=argv,
            exit_code=None if timed_out else process.returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            duration_ms=_elapsed_ms(start),
            truncated=out_truncated or err_truncated,
        )


def create_sandbox(
    backend: str,
    workspace: Path,
    *,
    image: str = DEFAULT_DOCKER_IMAGE,
    network: bool = False,
    memory: str = "1g",
    cpus: float = 1.0,
    pids_limit: int = 256,
    user: str = "10001:10001",
    docker_bin: str = "docker",
) -> Sandbox:
    """Build the configured execution sandbox for ``workspace``."""
    if backend == "local":
        return LocalSandbox(WorkspaceGuard(workspace))
    if backend == "docker":
        return DockerSandbox(
            workspace,
            image=image,
            network=network,
            memory=memory,
            cpus=cpus,
            pids_limit=pids_limit,
            user=user,
            docker_bin=docker_bin,
        )
    raise ValueError(f"unsupported sandbox backend: {backend}")
