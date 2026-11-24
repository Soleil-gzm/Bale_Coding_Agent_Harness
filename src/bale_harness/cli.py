import asyncio
from pathlib import Path
from typing import Annotated

import typer

from bale_harness.agent.loop import RunResult, build_default_loop
from bale_harness.contracts import ApprovalMode, Turn
from bale_harness.providers import create_provider
from bale_harness.settings import Settings
from bale_harness.turns import TurnRequest, build_turn

app = typer.Typer(no_args_is_help=True)


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
) -> None:
    """Execute a read-only ReAct run against the workspace."""
    settings = Settings()
    provider = create_provider(settings)
    loop = build_default_loop(provider, workspace)
    turn = Turn(
        message=message,
        workspace=workspace,
        token_budget=token_budget,
        max_iterations=max_iterations,
        repo_revision=repo_revision,
        approval_mode=approval_mode,
    )
    result: RunResult = asyncio.run(loop.run(turn))
    typer.echo(result.model_dump_json())
    raise typer.Exit(code=0 if result.status == "completed" else 1)
