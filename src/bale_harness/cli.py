import asyncio
import sys
from pathlib import Path
from typing import Annotated

import typer

from bale_harness.agent.approval import Decider, auto_approve
from bale_harness.agent.checkpoint import ShadowGit
from bale_harness.agent.loop import AgentLoop, RunResult, build_default_loop
from bale_harness.agent.messages import ToolCall, ToolSpec
from bale_harness.contracts import ApprovalMode, Turn
from bale_harness.providers import create_provider
from bale_harness.settings import Settings
from bale_harness.turns import TurnRequest, build_turn

app = typer.Typer(no_args_is_help=True)


def _console_decider(call: ToolCall, spec: ToolSpec) -> bool:
    """Ask a human to approve a side-effecting call; deny when not interactive."""
    if not sys.stdin.isatty():
        return False
    preview = ", ".join(f"{key}={value!r}" for key, value in call.arguments.items())
    return typer.confirm(f"Approve {spec.name}({preview})?")


async def _execute(
    loop: AgentLoop, turn: Turn, checkpoint: ShadowGit | None
) -> RunResult:
    try:
        return await loop.run(turn)
    finally:
        if checkpoint is not None:
            await checkpoint.cleanup()


@app.callback()
def main() -> None:
    """Bale Coding Agent Harness command line interface."""


@app.command("turn")
def turn_command(
    message: Annotated[str, typer.Option(..., help="Current user message")],
    workspace: Annotated[Path, typer.Option(..., help="Workspace path")],
    token_budget: Annotated[int, typer.Option(..., min=1)],
    repo_revision: Annotated[str | None, typer.Option()] = None,
    max_iterations: Annotated[int, typer.Option(min=1)] = 20,
    cost_budget: Annotated[float | None, typer.Option(min=0)] = None,
    approval_mode: Annotated[ApprovalMode, typer.Option()] = "approve_write",
) -> None:
    create_provider(Settings())
    request = TurnRequest(
        message=message,
        workspace=workspace,
        repo_revision=repo_revision,
        max_iterations=max_iterations,
        token_budget=token_budget,
        cost_budget=cost_budget,
        approval_mode=approval_mode,
    )
    typer.echo(build_turn(request).model_dump_json())


@app.command("run")
def run_command(
    message: Annotated[str, typer.Option(..., help="Current user message")],
    workspace: Annotated[
        Path,
        typer.Option(
            ...,
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
            resolve_path=True,
            help="Workspace directory to operate on",
        ),
    ],
    token_budget: Annotated[int, typer.Option(min=1)] = 128000,
    max_iterations: Annotated[int, typer.Option(min=1, max=500)] = 20,
    repo_revision: Annotated[str | None, typer.Option()] = None,
    approval_mode: Annotated[ApprovalMode, typer.Option()] = "approve_write",
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Approve every write and command without prompting"),
    ] = False,
    sandbox: Annotated[
        str, typer.Option(help="Execution sandbox to use (only 'local' for now)")
    ] = "local",
    shadow_git: Annotated[
        bool,
        typer.Option(
            "--shadow-git/--no-shadow-git",
            help="Snapshot the workspace in a private git directory",
        ),
    ] = True,
) -> None:
    """Execute a ReAct run against the workspace."""
    if sandbox != "local":
        raise typer.BadParameter(
            f"unsupported sandbox {sandbox!r}; only 'local' is available"
        )

    settings = Settings()
    provider = create_provider(settings)
    checkpoint = ShadowGit(workspace) if shadow_git else None
    approver: Decider = auto_approve if yes else _console_decider
    loop = build_default_loop(
        provider,
        workspace,
        approval_mode=approval_mode,
        approver=approver,
        checkpoint=checkpoint,
    )
    turn = Turn(
        message=message,
        workspace=workspace,
        token_budget=token_budget,
        max_iterations=max_iterations,
        repo_revision=repo_revision,
        approval_mode=approval_mode,
    )
    result: RunResult = asyncio.run(_execute(loop, turn, checkpoint))
    typer.echo(result.model_dump_json())
    raise typer.Exit(code=0 if result.status == "completed" else 1)
