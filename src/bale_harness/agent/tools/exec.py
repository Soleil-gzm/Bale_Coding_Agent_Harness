"""Shell and test tools backed by a sandbox adapter.

Commands are passed as an argument vector, never through a shell. A string
command is split with :mod:`shlex` and rejected if it contains shell
metacharacters, so pipelines, redirection, and chaining are impossible. Tools
with paths or spaces should use the list form.
"""

import shlex
import time
from pathlib import Path
from typing import Any

from bale_harness.agent.messages import ToolSpec
from bale_harness.agent.sandbox import (
    MAX_OUTPUT_CHARS,
    ExecResult,
    Sandbox,
    SandboxViolation,
)
from bale_harness.agent.security import WorkspaceError, WorkspaceGuard
from bale_harness.agent.tools.common import error, success, truncate
from bale_harness.agent.tools.registry import BaseTool
from bale_harness.agent.trace import SpanSink
from bale_harness.contracts import ToolResult

SHELL_METACHARACTERS = ("|", "&", ";", ">", "<", "`", "$(")

DENIED_PYTEST_OPTIONS = frozenset(
    {"-p", "-c", "-o", "--basetemp", "--rootdir", "--confcutdir", "--override-ini"}
)
_DENIED_SHORT_OPTIONS = ("-p", "-c", "-o")


def _unsafe_pytest_arg(args: list[str]) -> str | None:
    """Return a reason when an extra pytest argument could leave the workspace.

    Plugins (``-p``), config files (``-c``/``-o``/``--override-ini``) and temp
    or root overrides (``--basetemp``/``--rootdir``/``--confcutdir``) can make
    pytest execute or delete things outside the workspace, so they are denied
    outright. Absolute and ``..`` paths are denied for the same reason.
    """
    for arg in args:
        if arg in DENIED_PYTEST_OPTIONS:
            return f"pytest option not allowed: {arg}"
        if any(arg.startswith(f"{option}=") for option in DENIED_PYTEST_OPTIONS):
            return f"pytest option not allowed: {arg}"
        if len(arg) > 2 and arg.startswith(_DENIED_SHORT_OPTIONS):
            return f"pytest option not allowed: {arg}"
        value = arg.split("=", 1)[1] if arg.startswith("--") and "=" in arg else arg
        if value.startswith(("/", "\\")) or Path(value).is_absolute():
            return f"absolute path not allowed in args: {arg}"
        if ".." in Path(value).parts:
            return f"path traversal not allowed in args: {arg}"
    return None


async def _sandbox_run(
    sandbox: Sandbox,
    name: str,
    argv: list[str],
    *,
    span_sink: SpanSink | None,
    cwd: str = ".",
    timeout_s: float,
    extra_env: dict[str, str] | None = None,
) -> ExecResult:
    """Run one command and record a ``sandbox`` span around it."""
    started = time.perf_counter()
    try:
        result = await sandbox.run(
            argv, cwd=cwd, timeout_s=timeout_s, extra_env=extra_env
        )
    except SandboxViolation:
        await _emit_sandbox(span_sink, sandbox, name, "error", started, None)
        raise
    if result.timed_out:
        status = "timeout"
    elif result.exit_code == 0:
        status = "success"
    else:
        status = "failed"
    await _emit_sandbox(span_sink, sandbox, name, status, started, result)
    return result


async def _emit_sandbox(
    span_sink: SpanSink | None,
    sandbox: Sandbox,
    name: str,
    status: str,
    started: float,
    result: ExecResult | None,
) -> None:
    if span_sink is None:
        return
    data: dict[str, object] = {"backend": type(sandbox).__name__}
    if result is not None:
        data["exit_code"] = result.exit_code
        data["timed_out"] = result.timed_out
    await span_sink.emit_span(
        "sandbox",
        "sandbox",
        name=name,
        status=status,
        duration_ms=max(0, int((time.perf_counter() - started) * 1000)),
        data=data,
    )


def split_command(command: Any) -> list[str]:
    """Normalize a command argument into an argv list, rejecting shell syntax."""
    if isinstance(command, list):
        if not command or not all(isinstance(part, str) and part for part in command):
            raise ValueError("command must be a non-empty list of non-empty strings")
        return list(command)
    if not isinstance(command, str) or not command.strip():
        raise ValueError("command must be a non-empty string or list of strings")
    for marker in SHELL_METACHARACTERS:
        if marker in command:
            raise ValueError(
                f"shell metacharacter {marker!r} is not allowed; "
                "pass an argument list instead"
            )
    parts = shlex.split(command)
    if not parts:
        raise ValueError("command produced no arguments")
    return parts


def _combined_output(result: ExecResult) -> tuple[str, bool]:
    sections: list[str] = []
    if result.stdout.strip():
        sections.append(result.stdout.rstrip())
    if result.stderr.strip():
        sections.append(result.stderr.rstrip())
    content, truncated = truncate("\n".join(sections), MAX_OUTPUT_CHARS)
    return content, truncated or result.truncated


def _timeout_result(name: str, result: ExecResult, start: float) -> ToolResult:
    content, _ = _combined_output(result)
    message = f"command timed out after {result.duration_ms}ms"
    if content:
        message = f"{message}\n{content}"
    return ToolResult(
        call_id="",
        name=name,
        status="timeout",
        content=message,
        error_code="timeout",
        latency_ms=max(0, int((time.perf_counter() - start) * 1000)),
    )


class ShellTool(BaseTool):
    spec = ToolSpec(
        name="shell",
        description=(
            "Run a command without a shell. Pass an argument list, or a string "
            "without shell metacharacters."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ]
                },
                "cwd": {"type": "string"},
                "timeout_s": {"type": "number"},
            },
            "required": ["command"],
        },
        side_effect="execute",
        timeout_ms=120_000,
    )

    def __init__(self, sandbox: Sandbox, span_sink: SpanSink | None = None) -> None:
        self._sandbox = sandbox
        self._span_sink = span_sink

    def validate_arguments(self, arguments: dict[str, Any]) -> None:
        split_command(arguments.get("command"))
        cwd = arguments.get("cwd", ".")
        if not isinstance(cwd, str):
            raise ValueError("cwd must be a string")
        timeout_s = arguments.get("timeout_s", 60)
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, int | float):
            raise ValueError("timeout_s must be a number")
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        start = time.perf_counter()
        try:
            argv = split_command(arguments["command"])
        except ValueError as exc:
            return error("shell", str(exc), "invalid_arguments", start)
        cwd = arguments.get("cwd", ".") or "."
        timeout_s = float(arguments.get("timeout_s", 60))

        try:
            result = await _sandbox_run(
                self._sandbox,
                "shell",
                argv,
                span_sink=self._span_sink,
                cwd=cwd,
                timeout_s=timeout_s,
            )
        except SandboxViolation as exc:
            return error("shell", str(exc), "sandbox_violation", start)

        if result.timed_out:
            return _timeout_result("shell", result, start)

        content, truncated = _combined_output(result)
        metadata = {
            "exit_code": result.exit_code,
            "duration_ms": result.duration_ms,
            "command": result.command,
        }
        if result.exit_code == 0:
            return success(
                "shell",
                content or "(no output)",
                metadata=metadata,
                truncated=truncated,
                start=start,
            )
        return error(
            "shell",
            content or f"command failed with exit code {result.exit_code}",
            "command_failed",
            start,
        )


class RunTestsTool(BaseTool):
    spec = ToolSpec(
        name="run_tests",
        description="Run pytest in the workspace and report the outcome.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "args": {"type": "array", "items": {"type": "string"}},
                "timeout_s": {"type": "number"},
            },
            "required": [],
        },
        side_effect="execute",
        timeout_ms=300_000,
    )

    def __init__(
        self,
        sandbox: Sandbox,
        guard: WorkspaceGuard,
        span_sink: SpanSink | None = None,
    ) -> None:
        self._sandbox = sandbox
        self._guard = guard
        self._span_sink = span_sink

    def validate_arguments(self, arguments: dict[str, Any]) -> None:
        path = arguments.get("path", ".")
        if not isinstance(path, str):
            raise ValueError("path must be a string")
        extra = arguments.get("args", [])
        if not isinstance(extra, list) or not all(
            isinstance(item, str) for item in extra
        ):
            raise ValueError("args must be a list of strings")
        unsafe = _unsafe_pytest_arg(extra)
        if unsafe is not None:
            raise ValueError(unsafe)
        timeout_s = arguments.get("timeout_s", 240)
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, int | float):
            raise ValueError("timeout_s must be a number")
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        start = time.perf_counter()
        target = arguments.get("path", ".") or "."
        extra = list(arguments.get("args", []))
        unsafe = _unsafe_pytest_arg(extra)
        if unsafe is not None:
            return error("run_tests", unsafe, "invalid_arguments", start)
        try:
            resolved = self._guard.resolve(target)
        except WorkspaceError:
            return error(
                "run_tests",
                f"path escapes workspace: {target}",
                "path_escape",
                start,
            )
        if resolved == self._guard.root:
            target = "."
        else:
            target = resolved.relative_to(self._guard.root).as_posix()
        timeout_s = float(arguments.get("timeout_s", 240))
        argv = [
            self._sandbox.python_executable,
            "-m",
            "pytest",
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
            *extra,
            target,
        ]

        try:
            result = await _sandbox_run(
                self._sandbox,
                "run_tests",
                argv,
                span_sink=self._span_sink,
                timeout_s=timeout_s,
                # Keep the workspace clean: no __pycache__ from test imports.
                extra_env={"PYTHONDONTWRITEBYTECODE": "1"},
            )
        except SandboxViolation as exc:
            return error("run_tests", str(exc), "sandbox_violation", start)

        if result.timed_out:
            return _timeout_result("run_tests", result, start)

        content, truncated = _combined_output(result)
        metadata = {
            "exit_code": result.exit_code,
            "duration_ms": result.duration_ms,
            "path": target,
        }
        if result.exit_code == 0:
            return success(
                "run_tests",
                content or "tests passed",
                metadata=metadata,
                truncated=truncated,
                start=start,
            )
        if result.exit_code == 5:
            return error("run_tests", "no tests were collected", "no_tests", start)
        return error(
            "run_tests",
            content or f"tests failed with exit code {result.exit_code}",
            "tests_failed",
            start,
        )


def build_exec_tools(
    sandbox: Sandbox, guard: WorkspaceGuard, span_sink: SpanSink | None = None
) -> list[BaseTool]:
    """Return the command-executing tools wired to ``sandbox`` and ``guard``."""
    return [
        ShellTool(sandbox, span_sink),
        RunTestsTool(sandbox, guard, span_sink),
    ]
