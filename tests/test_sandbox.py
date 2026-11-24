import asyncio
import sys
from pathlib import Path

import pytest

from bale_harness.agent.sandbox import LocalSandbox, SandboxViolation
from bale_harness.agent.security import WorkspaceGuard


def _run(coro):
    return asyncio.run(coro)


def _sandbox(tmp_path: Path) -> LocalSandbox:
    return LocalSandbox(WorkspaceGuard(tmp_path))


def test_local_sandbox_runs_command_in_workspace(tmp_path: Path) -> None:
    result = _run(_sandbox(tmp_path).run([sys.executable, "-c", "print('hello')"]))

    assert result.exit_code == 0
    assert "hello" in result.stdout
    assert not result.timed_out
    assert result.duration_ms >= 0


def test_local_sandbox_reports_non_zero_exit(tmp_path: Path) -> None:
    result = _run(
        _sandbox(tmp_path).run([sys.executable, "-c", "import sys; sys.exit(3)"])
    )

    assert result.exit_code == 3
    assert not result.timed_out


def test_local_sandbox_enforces_timeout(tmp_path: Path) -> None:
    result = _run(
        _sandbox(tmp_path).run(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            timeout_s=0.3,
        )
    )

    assert result.timed_out
    assert result.exit_code is None


def test_local_sandbox_rejects_working_directory_escape(tmp_path: Path) -> None:
    with pytest.raises(SandboxViolation, match="escapes workspace"):
        _run(_sandbox(tmp_path).run([sys.executable, "-c", "pass"], cwd=".."))


def test_local_sandbox_rejects_missing_working_directory(tmp_path: Path) -> None:
    with pytest.raises(SandboxViolation, match="not a directory"):
        _run(_sandbox(tmp_path).run([sys.executable, "-c", "pass"], cwd="missing"))


def test_local_sandbox_does_not_forward_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BALE_TEST_SECRET", "do-not-leak")

    result = _run(
        _sandbox(tmp_path).run(
            [
                sys.executable,
                "-c",
                "import os; print(os.environ.get('BALE_TEST_SECRET', 'absent'))",
            ]
        )
    )

    assert "absent" in result.stdout
    assert "do-not-leak" not in result.stdout


def test_local_sandbox_denies_blocked_command(tmp_path: Path) -> None:
    with pytest.raises(SandboxViolation, match="denied by policy"):
        _run(_sandbox(tmp_path).run(["curl", "https://example.com"]))


def test_local_sandbox_truncates_large_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("bale_harness.agent.sandbox.MAX_OUTPUT_CHARS", 10)

    result = _run(_sandbox(tmp_path).run([sys.executable, "-c", "print('x' * 500)"]))

    assert result.truncated
    assert len(result.stdout) == 10
