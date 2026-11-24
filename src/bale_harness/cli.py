import asyncio
import json
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

import typer

from bale_harness.agent.approval import Decider, auto_approve
from bale_harness.agent.checkpoint import ShadowGit
from bale_harness.agent.cost import CostRates
from bale_harness.agent.loop import AgentLoop, RunResult, build_default_loop
from bale_harness.agent.messages import ToolCall, ToolSpec
from bale_harness.agent.report import build_report
from bale_harness.agent.session import SessionStore, TurnRecord
from bale_harness.agent.trace import JsonlTraceExporter, read_trace
from bale_harness.contracts import ApprovalMode, Turn
from bale_harness.providers import create_provider
from bale_harness.settings import Settings
from bale_harness.turns import TurnRequest, build_turn

app = typer.Typer(no_args_is_help=True)
session_app = typer.Typer(no_args_is_help=True, help="Inspect persisted sessions.")
app.add_typer(session_app, name="session")


def _console_decider(call: ToolCall, spec: ToolSpec) -> bool:
    """Ask a human to approve a side-effecting call; deny when not interactive."""
    if not sys.stdin.isatty():
        return False
    preview = ", ".join(f"{key}={value!r}" for key, value in call.arguments.items())
    return typer.confirm(f"Approve {spec.name}({preview})?")


async def _execute(
    loop: AgentLoop,
    turn: Turn,
    checkpoint: ShadowGit | None,
    *,
    resume: bool = False,
) -> RunResult:
    try:
        return await loop.run(turn, resume=resume)
    finally:
        if checkpoint is not None:
            await checkpoint.cleanup()


async def _load_resumable(session: SessionStore, session_id: UUID) -> TurnRecord | None:
    await session.init()
    return await session.load_resumable_turn(session_id)


def _cost_rates(settings: Settings) -> CostRates | None:
    """Build cost rates only when both prices are configured."""
    if settings.input_cost_per_1k is None or settings.output_cost_per_1k is None:
        return None
    return CostRates(
        input_per_1k=settings.input_cost_per_1k,
        output_per_1k=settings.output_cost_per_1k,
    )


async def _collect_report(
    store: SessionStore, session_id: UUID, trace_path: str | None
) -> str | None:
    turns = await store.list_turns(session_id)
    if not turns:
        return None
    executed = {turn.turn_id: await store.load_executed(turn.turn_id) for turn in turns}
    return build_report(
        session_id=session_id,
        workspace=turns[0].workspace,
        turns=turns,
        executed=executed,
        trace=read_trace(trace_path) if trace_path else [],
        memory=await store.load_memory(session_id),
    )


def _checkpoint_dir(
    session_db: Path,
    session_id: UUID,
    workspace: Path,
    explicit: Path | None,
) -> Path:
    """Pick a persistent shadow git dir that never lives inside the workspace."""
    if explicit is not None:
        return explicit.expanduser().resolve()
    candidate = (
        session_db.expanduser().resolve().parent / "checkpoints" / str(session_id)
    )
    if candidate.is_relative_to(workspace):
        return Path(tempfile.gettempdir()) / "bale" / "checkpoints" / str(session_id)
    return candidate


def _parse_session_id(value: str | None) -> UUID:
    if value is None:
        return uuid4()
    try:
        return UUID(value)
    except ValueError as error:
        raise typer.BadParameter(f"invalid session id: {value}") from error


async def _restore(shadow: ShadowGit, snapshot: str | None) -> str:
    baseline = await shadow.init()
    target = snapshot or baseline
    await shadow.restore(target)
    return target


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
    message: Annotated[str | None, typer.Option(help="Current user message")] = None,
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
    session_db: Annotated[
        Path | None,
        typer.Option(help="SQLite database used to persist session state"),
    ] = None,
    session_id: Annotated[
        str | None, typer.Option(help="Session identifier (UUID)")
    ] = None,
    resume: Annotated[
        bool,
        typer.Option(
            "--resume",
            help="Resume the latest unfinished turn in the given session",
        ),
    ] = False,
    trace: Annotated[
        str | None,
        typer.Option(help="Append JSONL trace events to this file"),
    ] = None,
    checkpoint_dir: Annotated[
        Path | None,
        typer.Option(help="Persistent shadow git directory (outside the workspace)"),
    ] = None,
) -> None:
    """Execute a ReAct run against the workspace."""
    if sandbox != "local":
        raise typer.BadParameter(
            f"unsupported sandbox {sandbox!r}; only 'local' is available"
        )
    if resume:
        if message is not None:
            raise typer.BadParameter("--resume cannot be combined with --message")
        if session_db is None or session_id is None:
            raise typer.BadParameter(
                "--resume requires both --session-db and --session-id"
            )
    elif message is None:
        raise typer.BadParameter("--message is required unless --resume is used")

    parsed_session_id = _parse_session_id(session_id)

    settings = Settings()
    provider = create_provider(settings)
    checkpoint: ShadowGit | None = None
    if shadow_git:
        if checkpoint_dir is not None or session_db is not None:
            directory = _checkpoint_dir(
                session_db or Path.cwd(),
                parsed_session_id,
                workspace,
                checkpoint_dir,
            )
            checkpoint = ShadowGit(workspace, directory)
        else:
            checkpoint = ShadowGit(workspace)
    approver: Decider = auto_approve if yes else _console_decider
    session = SessionStore(session_db) if session_db is not None else None
    trace_path = trace or settings.trace_path
    tracer = JsonlTraceExporter(trace_path) if trace_path else None
    loop = build_default_loop(
        provider,
        workspace,
        approval_mode=approval_mode,
        approver=approver,
        checkpoint=checkpoint,
        session=session,
        context_window=settings.context_window,
        tracer=tracer,
        cost_rates=_cost_rates(settings),
    )

    if resume:
        assert session is not None
        record = asyncio.run(_load_resumable(session, parsed_session_id))
        if record is None:
            raise typer.BadParameter(
                f"no unfinished turn for session {parsed_session_id}"
            )
        if Path(record.workspace) != workspace:
            raise typer.BadParameter(f"session belongs to workspace {record.workspace}")
        turn = record.to_turn()
    else:
        assert message is not None
        turn = Turn(
            message=message,
            workspace=workspace,
            session_id=parsed_session_id,
            token_budget=token_budget,
            max_iterations=max_iterations,
            repo_revision=repo_revision,
            approval_mode=approval_mode,
        )

    result: RunResult = asyncio.run(_execute(loop, turn, checkpoint, resume=resume))
    typer.echo(result.model_dump_json())
    raise typer.Exit(code=0 if result.status == "completed" else 1)


@app.command("report")
def report_command(
    session_db: Annotated[
        Path,
        typer.Option(
            ...,
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            help="SQLite database with persisted session state",
        ),
    ],
    session_id: Annotated[str, typer.Option(help="Session identifier (UUID)")],
    trace: Annotated[
        Path | None, typer.Option(help="JSONL trace file to summarize")
    ] = None,
    output: Annotated[
        Path | None, typer.Option(help="Write the Markdown report to this file")
    ] = None,
) -> None:
    """Render a Markdown report for a persisted session."""
    try:
        parsed_session_id = UUID(session_id)
    except ValueError as error:
        raise typer.BadParameter(f"invalid session id: {session_id}") from error
    if trace is not None and not trace.exists():
        raise typer.BadParameter(f"trace file not found: {trace}")

    markdown = asyncio.run(
        _collect_report(
            SessionStore(session_db),
            parsed_session_id,
            str(trace) if trace is not None else None,
        )
    )
    if markdown is None:
        raise typer.BadParameter(f"no turns for session {parsed_session_id}")

    if output is None:
        typer.echo(markdown)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(markdown, encoding="utf-8")
    typer.echo(f"wrote {output}")


@session_app.command("list")
def session_list(
    session_db: Annotated[
        Path,
        typer.Option(
            ...,
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            help="SQLite database with persisted session state",
        ),
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List stored sessions, most recently updated first."""
    sessions = asyncio.run(SessionStore(session_db).list_sessions())
    if json_output:
        typer.echo(
            json.dumps(
                [session.model_dump(mode="json") for session in sessions],
                ensure_ascii=False,
            )
        )
        return
    for session in sessions:
        typer.echo(
            f"{session.session_id}  turns={session.turns}"
            f"  updated={session.updated_at}  workspace={session.workspace}"
        )


@session_app.command("show")
def session_show(
    session_db: Annotated[
        Path,
        typer.Option(
            ...,
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            help="SQLite database with persisted session state",
        ),
    ],
    session_id: Annotated[str, typer.Option(help="Session identifier (UUID)")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show the turns stored for one session."""
    parsed = _parse_session_id(session_id)
    turns = asyncio.run(SessionStore(session_db).list_turns(parsed))
    if not turns:
        raise typer.BadParameter(f"no turns for session {parsed}")
    if json_output:
        typer.echo(
            json.dumps(
                [turn.model_dump(mode="json") for turn in turns],
                ensure_ascii=False,
            )
        )
        return
    typer.echo(f"session {parsed}")
    typer.echo(f"workspace {turns[0].workspace}")
    for turn in turns:
        typer.echo(
            f"{turn.turn_id}  {turn.status:<9} iterations={turn.iteration}"
            f" tools={turn.tool_calls} tokens={turn.usage.total_tokens}"
            f"  {turn.message}"
        )


@app.command("trace")
def trace_command(
    trace: Annotated[
        Path,
        typer.Option(
            ...,
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            help="JSONL trace file to inspect",
        ),
    ],
    limit: Annotated[
        int, typer.Option(min=0, help="Print the last N events as JSON lines")
    ] = 0,
) -> None:
    """Summarize a JSONL trace file."""
    events = read_trace(trace)
    if not events:
        typer.echo("no events")
        return
    counts = Counter(event.event for event in events)
    typer.echo(f"events {len(events)}")
    for name in sorted(counts):
        typer.echo(f"{name} {counts[name]}")
    model_ms = sum(
        event.duration_ms or 0
        for event in events
        if event.event == "model_call" and event.duration_ms is not None
    )
    tool_ms = sum(
        event.duration_ms or 0
        for event in events
        if event.event == "tool_call" and event.duration_ms is not None
    )
    typer.echo(f"model_ms {model_ms}")
    typer.echo(f"tool_ms {tool_ms}")
    for event in events[-limit:] if limit else []:
        typer.echo(event.model_dump_json())


@app.command("chat")
def chat_command(
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
    session_db: Annotated[
        Path | None,
        typer.Option(help="SQLite database used to persist session state"),
    ] = None,
    session_id: Annotated[
        str | None, typer.Option(help="Session identifier (UUID)")
    ] = None,
    checkpoint_dir: Annotated[
        Path | None,
        typer.Option(help="Persistent shadow git directory (outside the workspace)"),
    ] = None,
) -> None:
    """Run an interactive multi-turn session; type /exit to quit."""
    if sandbox != "local":
        raise typer.BadParameter(
            f"unsupported sandbox {sandbox!r}; only 'local' is available"
        )
    parsed_session_id = _parse_session_id(session_id)
    settings = Settings()
    provider = create_provider(settings)
    checkpoint: ShadowGit | None = None
    if shadow_git:
        if checkpoint_dir is not None or session_db is not None:
            checkpoint = ShadowGit(
                workspace,
                _checkpoint_dir(
                    session_db or Path.cwd(),
                    parsed_session_id,
                    workspace,
                    checkpoint_dir,
                ),
            )
        else:
            checkpoint = ShadowGit(workspace)
    approver: Decider = auto_approve if yes else _console_decider
    loop = build_default_loop(
        provider,
        workspace,
        approval_mode=approval_mode,
        approver=approver,
        checkpoint=checkpoint,
        session=SessionStore(session_db) if session_db is not None else None,
        context_window=settings.context_window,
        cost_rates=_cost_rates(settings),
    )
    typer.echo(f"session {parsed_session_id} (type /exit to quit)")
    try:
        while True:
            try:
                message = typer.prompt("you", prompt_suffix="> ")
            except (EOFError, KeyboardInterrupt):
                typer.echo("")
                break
            if message.strip() in {"/exit", "/quit"}:
                break
            if not message.strip():
                continue
            turn = Turn(
                message=message,
                workspace=workspace,
                session_id=parsed_session_id,
                token_budget=token_budget,
                max_iterations=max_iterations,
                approval_mode=approval_mode,
            )
            result: RunResult = asyncio.run(loop.run(turn))
            typer.echo(
                result.answer or f"[{result.status}] {result.error or ''}".rstrip()
            )
    finally:
        if checkpoint is not None:
            asyncio.run(checkpoint.cleanup())


@app.command("undo")
def undo_command(
    workspace: Annotated[
        Path,
        typer.Option(
            ...,
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
            resolve_path=True,
            help="Workspace directory to restore",
        ),
    ],
    session_id: Annotated[str, typer.Option(help="Session identifier (UUID)")],
    session_db: Annotated[
        Path | None,
        typer.Option(help="Session database used to derive the checkpoint directory"),
    ] = None,
    checkpoint_dir: Annotated[
        Path | None, typer.Option(help="Checkpoint directory to restore from")
    ] = None,
    snapshot: Annotated[
        str | None, typer.Option(help="Snapshot commit to restore (default: baseline)")
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", help="Restore without asking for confirmation")
    ] = False,
) -> None:
    """Restore the workspace from a persisted shadow git checkpoint."""
    parsed = _parse_session_id(session_id)
    if checkpoint_dir is None and session_db is None:
        raise typer.BadParameter("--checkpoint-dir or --session-db is required")
    directory = _checkpoint_dir(
        session_db or Path.cwd(), parsed, workspace, checkpoint_dir
    )
    if not (directory / "HEAD").exists():
        raise typer.BadParameter(f"no checkpoint at {directory}")
    if not yes and not typer.confirm(
        f"Restore {workspace} to {snapshot or 'the baseline'}?"
    ):
        raise typer.Exit(code=1)
    target = asyncio.run(_restore(ShadowGit(workspace, directory), snapshot))
    typer.echo(f"restored {workspace} to {target}")
