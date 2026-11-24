from pathlib import Path
from typing import Annotated

import typer

from bale_harness.contracts import ApprovalMode
from bale_harness.providers import create_provider
from bale_harness.settings import Settings
from bale_harness.turns import TurnRequest, build_turn

app = typer.Typer(no_args_is_help=True)


@app.callback()
def main() -> None:
    """Create Bale Harness turns without executing them."""


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
