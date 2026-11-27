import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

from bale_harness.agent.sandbox import ExecResult, LocalSandbox
from bale_harness.agent.security import WorkspaceGuard
from bale_harness.agent.tools.exec import RunTestsTool, ShellTool, split_command


def _run(tool: Any, arguments: dict[str, Any]):
    return asyncio.run(tool.execute(arguments))


def _shell(tmp_path: Path) -> ShellTool:
    return ShellTool(LocalSandbox(WorkspaceGuard(tmp_path)))


def _run_tests(tmp_path: Path) -> RunTestsTool:
    guard = WorkspaceGuard(tmp_path)
    return RunTestsTool(LocalSandbox(guard), guard)


def test_split_command_accepts_list_and_string() -> None:
    assert split_command(["python", "-V"]) == ["python", "-V"]
    assert split_command('python -c "print(1)"') == ["python", "-c", "print(1)"]


@pytest.mark.parametrize("command", ["echo hi && whoami", "cat a | b", "ls > out"])
def test_split_command_rejects_metacharacters(command: str) -> None:
    with pytest.raises(ValueError, match="metacharacter"):
        split_command(command)


def test_shell_runs_argv_list(tmp_path: Path) -> None:
    result = _run(_shell(tmp_path), {"command": [sys.executable, "-c", "print(7)"]})

    assert result.status == "success"
    assert result.metadata["exit_code"] == 0
    assert "7" in result.content


def test_shell_reports_non_zero_exit(tmp_path: Path) -> None:
    result = _run(
        _shell(tmp_path),
        {"command": [sys.executable, "-c", "import sys; sys.exit(2)"]},
    )

    assert result.status == "error"
    assert result.error_code == "command_failed"


def test_shell_reports_timeout(tmp_path: Path) -> None:
    result = _run(
        _shell(tmp_path),
        {
            "command": [sys.executable, "-c", "import time; time.sleep(5)"],
            "timeout_s": 0.3,
        },
    )

    assert result.status == "timeout"
    assert result.error_code == "timeout"


def test_shell_surfaces_sandbox_violation(tmp_path: Path) -> None:
    result = _run(_shell(tmp_path), {"command": ["curl", "https://example.com"]})

    assert result.status == "error"
    assert result.error_code == "sandbox_violation"


def test_shell_rejects_cwd_escape(tmp_path: Path) -> None:
    result = _run(
        _shell(tmp_path),
        {"command": [sys.executable, "-c", "pass"], "cwd": ".."},
    )

    assert result.status == "error"
    assert result.error_code == "sandbox_violation"


def test_shell_rejects_metacharacter_string(tmp_path: Path) -> None:
    result = _run(_shell(tmp_path), {"command": "echo hi && whoami"})

    assert result.status == "error"
    assert result.error_code == "invalid_arguments"


def test_run_tests_reports_success(tmp_path: Path) -> None:
    (tmp_path / "test_ok.py").write_bytes(b"def test_ok():\n    assert 1 + 1 == 2\n")

    result = _run(_run_tests(tmp_path), {"path": "."})

    assert result.status == "success"
    assert result.metadata["exit_code"] == 0


def test_run_tests_reports_failure(tmp_path: Path) -> None:
    (tmp_path / "test_bad.py").write_bytes(b"def test_bad():\n    assert False\n")

    result = _run(_run_tests(tmp_path), {"path": "."})

    assert result.status == "error"
    assert result.error_code == "tests_failed"
    assert "1 failed" in result.content


def test_run_tests_reports_no_tests(tmp_path: Path) -> None:
    result = _run(_run_tests(tmp_path), {"path": "."})

    assert result.status == "error"
    assert result.error_code == "no_tests"


class _RecordingSandbox:
    python_executable = "python-in-container"

    def __init__(self) -> None:
        self.command: list[str] | None = None

    async def run(self, command: list[str], **_: Any) -> ExecResult:
        self.command = command
        return ExecResult(command=command, exit_code=0, stdout="ok")


def test_run_tests_uses_the_sandbox_python(tmp_path: Path) -> None:
    sandbox = _RecordingSandbox()

    result = _run(RunTestsTool(sandbox, WorkspaceGuard(tmp_path)), {"path": "."})

    assert result.status == "success"
    assert sandbox.command is not None
    assert sandbox.command[0] == "python-in-container"


def test_run_tests_rejects_paths_outside_the_workspace(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside-tests"
    outside.mkdir()

    for target in ("..", str(outside)):
        result = _run(_run_tests(tmp_path), {"path": target})
        assert result.status == "error", target
        assert result.error_code == "path_escape", target


@pytest.mark.parametrize(
    "extra",
    [
        ["-p", "evil_plugin"],
        ["-pno:cacheprovider"],
        ["-c", "pytest.ini"],
        ["-o", "addopts=-p evil_plugin"],
        ["--basetemp", "tmpdir"],
        ["--rootdir=/"],
        ["--confcutdir", ".."],
        ["/etc/pytest_plugin.py"],
        ["..\\outside\\test_x.py"],
        ["--deselect=/outside/test_x.py"],
    ],
)
def test_run_tests_rejects_dangerous_args(tmp_path: Path, extra: list[str]) -> None:
    result = _run(_run_tests(tmp_path), {"path": ".", "args": extra})

    assert result.status == "error"
    assert result.error_code == "invalid_arguments"


def test_run_tests_allows_safe_args(tmp_path: Path) -> None:
    (tmp_path / "test_ok.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )

    result = _run(
        _run_tests(tmp_path),
        {"path": ".", "args": ["-k", "test_ok"]},
    )

    assert result.status == "success", result.content
