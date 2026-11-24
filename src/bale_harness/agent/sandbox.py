"""Sandbox execution port and a local subprocess adapter.

:class:`LocalSandbox` runs commands directly on the host. Its protections are
layered and best-effort only: no shell interpretation, a command deny-list, an
environment-variable allowlist, a workspace-bound working directory, an
enforced timeout, and output truncation.

It is **not** a security boundary. Untrusted code must run in an isolated
container or virtual machine; the Docker adapter is a later milestone.
"""

import asyncio
import os
import subprocess
import time
from pathlib import Path
from typing import Protocol

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
