import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

from bale_harness.agent.sandbox import LocalSandbox
from bale_harness.agent.security import WorkspaceGuard
from bale_harness.agent.tools.exec import RunTestsTool, ShellTool, split_command


def _run(tool: Any, arguments: dict[str, Any]):
    return asyncio.run(tool.execute(arguments))


def _shell(tmp_path: Path) -> ShellTool:
    return ShellTool(LocalSandbox(WorkspaceGuard(tmp_path)))


def _run_tests(tmp_path: Path) -> RunTestsTool:
    return RunTestsTool(LocalSandbox(WorkspaceGuard(tmp_path)))


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
