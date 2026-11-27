import asyncio
import os
import shutil
import types
from pathlib import Path

import pytest

from bale_harness.agent.approval import auto_approve
from bale_harness.agent.loop import build_default_loop
from bale_harness.agent.messages import ModelResponse, ToolCall
from bale_harness.agent.sandbox import (
    DockerSandbox,
    LocalSandbox,
    SandboxViolation,
    parse_docker_inspect,
)
from bale_harness.contracts import Turn
from bale_harness.providers import FakeProvider


def _run(coro):
    return asyncio.run(coro)


def _flag_value(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


def test_build_command_hardens_the_container(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    sandbox = DockerSandbox(tmp_path, docker_bin="docker")

    command = sandbox.build_command(
        ["python", "-c", "print(1)"],
        cwd="pkg",
        extra_env={"PYTHONDONTWRITEBYTECODE": "1"},
        name="bale-test",
    )

    assert command[0] == "docker"
    assert command[1:4] == ["run", "--rm", "--init"]
    assert _flag_value(command, "--network") == "none"
    assert _flag_value(command, "--workdir") == "/workspace/pkg"
    assert _flag_value(command, "--user") == "10001:10001"
    assert _flag_value(command, "--memory") == "1g"
    assert _flag_value(command, "--cpus") == "1"
    assert _flag_value(command, "--pids-limit") == "256"
    assert _flag_value(command, "--security-opt") == "no-new-privileges"
    assert _flag_value(command, "--cap-drop") == "ALL"
    assert "--read-only" in command
    assert _flag_value(command, "--tmpfs") == "/tmp:rw,size=64m"
    assert f"{tmp_path.resolve()}:/workspace" in command
    assert "PYTHONDONTWRITEBYTECODE=1" in command
    assert command[-4:] == [
        "python:3.11-slim",
        "python",
        "-c",
        "print(1)",
    ]


def test_build_command_uses_workspace_root_for_dot(tmp_path: Path) -> None:
    sandbox = DockerSandbox(tmp_path, docker_bin="docker")

    command = sandbox.build_command(["echo", "ok"], name="bale-test")

    assert _flag_value(command, "--workdir") == "/workspace"


def test_build_command_can_enable_network(tmp_path: Path) -> None:
    sandbox = DockerSandbox(tmp_path, network=True, docker_bin="docker")

    command = sandbox.build_command(["echo", "ok"], name="bale-test")

    assert _flag_value(command, "--network") == "bridge"


def test_build_command_rejects_working_directory_escape(tmp_path: Path) -> None:
    sandbox = DockerSandbox(tmp_path, docker_bin="docker")

    with pytest.raises(SandboxViolation, match="escapes workspace"):
        sandbox.build_command(["echo", "ok"], cwd="..", name="bale-test")


def test_build_command_rejects_missing_working_directory(tmp_path: Path) -> None:
    sandbox = DockerSandbox(tmp_path, docker_bin="docker")

    with pytest.raises(SandboxViolation, match="not a directory"):
        sandbox.build_command(["echo", "ok"], cwd="missing", name="bale-test")


def test_missing_docker_binary_raises_violation(tmp_path: Path) -> None:
    sandbox = DockerSandbox(tmp_path, docker_bin="bale-no-such-docker-binary")

    with pytest.raises(SandboxViolation, match="docker executable not found"):
        _run(sandbox.run(["python", "-c", "print(1)"]))


@pytest.mark.skipif(
    os.environ.get("BALE_TEST_DOCKER") != "1",
    reason="set BALE_TEST_DOCKER=1 to run Docker integration tests",
)
def test_docker_sandbox_runs_command(tmp_path: Path) -> None:
    image = os.environ.get("BALE_TEST_DOCKER_IMAGE", "python:3.11-slim")
    sandbox = DockerSandbox(tmp_path, image=image)

    result = _run(
        sandbox.run(
            ["python", "-c", "print('hello from container')"],
            timeout_s=120,
        )
    )

    assert result.exit_code == 0
    assert "hello from container" in result.stdout
    assert not result.timed_out


@pytest.mark.skipif(
    os.environ.get("BALE_TEST_DOCKER") != "1" or shutil.which("docker") is None,
    reason="set BALE_TEST_DOCKER=1 and install Docker to run integration tests",
)
def test_docker_sandbox_does_not_forward_host_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BALE_TEST_SECRET", "do-not-leak")
    image = os.environ.get("BALE_TEST_DOCKER_IMAGE", "python:3.11-slim")
    sandbox = DockerSandbox(tmp_path, image=image)

    result = _run(
        sandbox.run(
            [
                "python",
                "-c",
                "import os; print(os.environ.get('BALE_TEST_SECRET', 'absent'))",
            ],
            timeout_s=120,
        )
    )

    assert "absent" in result.stdout
    assert "do-not-leak" not in result.stdout


@pytest.mark.skipif(
    os.environ.get("BALE_TEST_DOCKER") != "1",
    reason="set BALE_TEST_DOCKER=1 to run Docker integration tests",
)
def test_agent_loop_runs_shell_in_docker(tmp_path: Path) -> None:
    image = os.environ.get("BALE_TEST_DOCKER_IMAGE", "python:3.11-slim")
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        call_id="",
                        name="shell",
                        arguments={
                            "command": [
                                "python",
                                "-c",
                                "print('sandbox-ok')",
                            ]
                        },
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        call_id="",
                        name="finish",
                        arguments={"message": "container command passed"},
                    )
                ]
            ),
        ]
    )
    loop = build_default_loop(
        provider,
        tmp_path,
        approver=auto_approve,
        sandbox=DockerSandbox(tmp_path, image=image),
    )

    result = _run(
        loop.run(
            Turn(
                message="run a command",
                workspace=tmp_path,
                token_budget=4096,
                max_iterations=3,
            )
        )
    )

    assert result.status == "completed"
    assert result.answer == "container command passed"
    assert result.tool_calls == 2
    second_prompt = provider.calls[1][0]
    assert any(
        message.role == "tool" and "sandbox-ok" in message.content
        for message in second_prompt
    )


def test_parse_docker_inspect_extracts_digest() -> None:
    assert parse_docker_inspect("sha256:deadbeef") == "sha256:deadbeef"
    assert parse_docker_inspect("python:3.11-slim@sha256:abc\n") == "sha256:abc"
    assert parse_docker_inspect("") is None
    assert parse_docker_inspect("garbage output") is None


def test_docker_sandbox_image_digest_resolves_and_caches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple] = []

    def fake_run(*args: object, **kwargs: object) -> object:
        calls.append(args)
        return types.SimpleNamespace(
            returncode=0, stdout="sha256:resolved\n", stderr=""
        )

    monkeypatch.setattr("bale_harness.agent.sandbox.subprocess.run", fake_run)
    sandbox = DockerSandbox(tmp_path, docker_bin="docker")
    assert sandbox.image_digest == "sha256:resolved"
    assert sandbox.image_digest == "sha256:resolved"
    assert len(calls) == 1


def test_docker_sandbox_image_digest_none_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(*args: object, **kwargs: object) -> object:
        return types.SimpleNamespace(returncode=1, stdout="", stderr="missing")

    monkeypatch.setattr("bale_harness.agent.sandbox.subprocess.run", fake_run)
    assert DockerSandbox(tmp_path, docker_bin="docker").image_digest is None


def test_local_sandbox_image_digest_is_none(tmp_path: Path) -> None:
    assert LocalSandbox(tmp_path).image_digest is None
