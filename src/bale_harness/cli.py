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
from bale_harness.agent.sandbox import create_sandbox
from bale_harness.agent.security import WorkspaceError, WorkspaceGuard
from bale_harness.agent.session import SessionStore, TurnRecord
from bale_harness.agent.tools.code_search import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    format_hits,
)
from bale_harness.agent.tools.mcp import (
    McpConfigError,
    McpServerConfig,
    load_mcp_config,
    mcp_tools,
)
from bale_harness.agent.trace import JsonlTraceExporter, read_trace
from bale_harness.contracts import ApprovalMode, Turn
from bale_harness.eval.report import render_markdown
from bale_harness.eval.runner import EvalReport, run_tasks
from bale_harness.eval.task import EvalTaskError, load_tasks, resolve_task_file
from bale_harness.providers import create_provider
from bale_harness.repo_index import HybridIndex, create_embedder, create_retriever
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
    time_budget: Annotated[float | None, typer.Option(min=0.001)] = None,
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
        time_budget=time_budget,
        approval_mode=approval_mode,
    )
    typer.echo(build_turn(request).model_dump_json())


@app.command("search")
def search_command(
    workspace: Annotated[
        Path,
        typer.Option(
            ...,
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
            resolve_path=True,
            help="Workspace directory to index and search",
        ),
    ],
    query: Annotated[str, typer.Option(..., help="Query text or identifiers")],
    limit: Annotated[
        int, typer.Option(min=1, max=MAX_LIMIT, help="Maximum number of hits")
    ] = DEFAULT_LIMIT,
    path: Annotated[
        str, typer.Option(help="Subdirectory to search, relative to the workspace")
    ] = ".",
    json_output: Annotated[
        bool, typer.Option("--json", help="Print hits as JSON")
    ] = False,
) -> None:
    """Rank workspace code chunks with a BM25 lexical index."""
    guard = WorkspaceGuard(workspace)
    try:
        root = guard.resolve(path)
    except WorkspaceError as error:
        raise typer.BadParameter(f"path escapes workspace: {path}") from error
    if not root.exists():
        raise typer.BadParameter(f"path not found: {path}")
    if not root.is_dir():
        raise typer.BadParameter(f"not a directory: {path}")

    settings = Settings()
    index = HybridIndex(
        root,
        embedder=create_embedder(settings),
        alpha=settings.retrieval_alpha,
        index_path=settings.retrieval_index_path,
    )
    hits = index.search(query, limit)
    if json_output:
        typer.echo(
            json.dumps(
                {
                    "query": query,
                    "path": path,
                    "indexed_files": index.file_count,
                    "indexed_chunks": index.chunk_count,
                    "vector_search": index.vector_enabled,
                    "hits": [
                        {
                            "path": hit.path,
                            "start_line": hit.start_line,
                            "end_line": hit.end_line,
                            "score": hit.score,
                            "snippet": hit.snippet,
                        }
                        for hit in hits
                    ],
                },
                ensure_ascii=False,
            )
        )
        return
    if not hits:
        typer.echo(f"no matches for {query.strip()!r}")
        return
    typer.echo(format_hits(hits))


@app.command("eval")
def eval_command(
    tasks: Annotated[
        Path,
        typer.Option(
            ...,
            exists=True,
            readable=True,
            help="JSON task file or directory containing tasks.json",
        ),
    ],
    output: Annotated[
        Path | None, typer.Option(help="Write the Markdown report to this file")
    ] = None,
    json_output: Annotated[
        Path | None, typer.Option("--json", help="Write the JSON report to this file")
    ] = None,
) -> None:
    """Run fixed evaluation tasks on fixture copies and verify their checks."""
    try:
        task_list = load_tasks(tasks)
    except EvalTaskError as error:
        raise typer.BadParameter(str(error)) from error
    task_file = resolve_task_file(tasks)
    if not task_list:
        raise typer.BadParameter(f"no tasks in {task_file}")

    settings = Settings()
    report: EvalReport = asyncio.run(
        run_tasks(
            task_list,
            task_file.parent,
            lambda _task: create_provider(settings),
        )
    )
    markdown = render_markdown(report)
    if output is None:
        typer.echo(markdown)
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(markdown, encoding="utf-8")
    if json_output is not None:
        json_output.parent.mkdir(parents=True, exist_ok=True)
        json_output.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    typer.echo(
        f"passed {report.passed}/{report.total} "
        f"(failed {report.failed}, errors {report.errors})"
    )
    if not report.ok:
        raise typer.Exit(code=1)


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
    time_budget: Annotated[
        float | None,
        typer.Option(
            "--time-budget",
            min=0.001,
            help="Wall-clock budget in seconds for the whole run",
        ),
    ] = None,
    repo_revision: Annotated[str | None, typer.Option()] = None,
    approval_mode: Annotated[ApprovalMode, typer.Option()] = "approve_write",
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Approve every write and command without prompting"),
    ] = False,
    sandbox: Annotated[
        str | None,
        typer.Option(help="Execution sandbox: 'local' or 'docker'"),
    ] = None,
    sandbox_image: Annotated[
        str | None,
        typer.Option("--sandbox-image", help="Container image for the docker sandbox"),
    ] = None,
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
    mcp_config: Annotated[
        Path | None,
        typer.Option(
            "--mcp-config",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            help="JSON file describing stdio MCP servers",
        ),
    ] = None,
) -> None:
    """Execute a ReAct run against the workspace."""
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
    backend = sandbox or settings.sandbox_backend
    if backend not in {"local", "docker"}:
        raise typer.BadParameter(
            f"unsupported sandbox {backend!r}; use 'local' or 'docker'"
        )
    exec_sandbox = create_sandbox(
        backend,
        workspace,
        image=sandbox_image or settings.sandbox_image,
        network=settings.sandbox_network,
        memory=settings.sandbox_memory,
        cpus=settings.sandbox_cpus,
        pids_limit=settings.sandbox_pids_limit,
        user=settings.sandbox_user,
    )
    provider = create_provider(settings)
    mcp_servers: list[McpServerConfig] = []
    if mcp_config is not None:
        try:
            mcp_servers = load_mcp_config(mcp_config)
        except McpConfigError as error:
            raise typer.BadParameter(str(error)) from error
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
    retriever = create_retriever(settings, workspace)
    search_embedder = create_embedder(settings)

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
            time_budget=time_budget,
            repo_revision=repo_revision,
            approval_mode=approval_mode,
        )

    async def _run_with_tools() -> RunResult:
        async with mcp_tools(mcp_servers) as extra_tools:
            loop = build_default_loop(
                provider,
                workspace,
                approval_mode=approval_mode,
                approver=approver,
                sandbox=exec_sandbox,
                checkpoint=checkpoint,
                session=session,
                context_window=settings.context_window,
                tracer=tracer,
                cost_rates=_cost_rates(settings),
                extra_tools=extra_tools,
                retriever=retriever,
                search_embedder=search_embedder,
                search_alpha=settings.retrieval_alpha,
                search_index_path=settings.retrieval_index_path,
            )
            return await _execute(loop, turn, checkpoint, resume=resume)

    result: RunResult = asyncio.run(_run_with_tools())
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
    time_budget: Annotated[
        float | None,
        typer.Option(
            "--time-budget",
            min=0.001,
            help="Wall-clock budget in seconds for each turn",
        ),
    ] = None,
    approval_mode: Annotated[ApprovalMode, typer.Option()] = "approve_write",
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Approve every write and command without prompting"),
    ] = False,
    sandbox: Annotated[
        str | None,
        typer.Option(help="Execution sandbox: 'local' or 'docker'"),
    ] = None,
    sandbox_image: Annotated[
        str | None,
        typer.Option("--sandbox-image", help="Container image for the docker sandbox"),
    ] = None,
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
    mcp_config: Annotated[
        Path | None,
        typer.Option(
            "--mcp-config",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            help="JSON file describing stdio MCP servers",
        ),
    ] = None,
) -> None:
    """Run an interactive multi-turn session; type /exit to quit."""
    parsed_session_id = _parse_session_id(session_id)
    settings = Settings()
    backend = sandbox or settings.sandbox_backend
    if backend not in {"local", "docker"}:
        raise typer.BadParameter(
            f"unsupported sandbox {backend!r}; use 'local' or 'docker'"
        )
    exec_sandbox = create_sandbox(
        backend,
        workspace,
        image=sandbox_image or settings.sandbox_image,
        network=settings.sandbox_network,
        memory=settings.sandbox_memory,
        cpus=settings.sandbox_cpus,
        pids_limit=settings.sandbox_pids_limit,
        user=settings.sandbox_user,
    )
    provider = create_provider(settings)
    mcp_servers: list[McpServerConfig] = []
    if mcp_config is not None:
        try:
            mcp_servers = load_mcp_config(mcp_config)
        except McpConfigError as error:
            raise typer.BadParameter(str(error)) from error
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
    retriever = create_retriever(settings, workspace)
    search_embedder = create_embedder(settings)

    async def _drive() -> None:
        """Keep one async context alive so MCP sessions span every turn."""
        async with mcp_tools(mcp_servers) as extra_tools:
            loop = build_default_loop(
                provider,
                workspace,
                approval_mode=approval_mode,
                approver=approver,
                sandbox=exec_sandbox,
                checkpoint=checkpoint,
                session=SessionStore(session_db) if session_db is not None else None,
                context_window=settings.context_window,
                cost_rates=_cost_rates(settings),
                extra_tools=extra_tools,
                retriever=retriever,
                search_embedder=search_embedder,
                search_alpha=settings.retrieval_alpha,
                search_index_path=settings.retrieval_index_path,
            )
            typer.echo(f"session {parsed_session_id} (type /exit to quit)")
            while True:
                try:
                    message = await asyncio.to_thread(
                        typer.prompt, "you", prompt_suffix="> "
                    )
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
                    time_budget=time_budget,
                    approval_mode=approval_mode,
                )
                result: RunResult = await loop.run(turn)
                typer.echo(
                    result.answer or f"[{result.status}] {result.error or ''}".rstrip()
                )

    try:
        asyncio.run(_drive())
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
