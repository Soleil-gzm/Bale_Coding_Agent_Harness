"""FastAPI entry points for the harness.

``POST /v1/turns`` only constructs a Turn. ``POST /v1/runs`` executes one turn
and returns the final :class:`RunResult`; ``POST /v1/runs/stream`` executes the
same turn and streams trace events as Server-Sent Events, ending with a
``result`` event. ``GET /health`` is public; every ``/v1`` route requires a
bearer token when ``BALE_HTTP_API_KEY`` is set. ``POST /v1/runs/{run_id}/cancel``
cooperatively cancels an in-flight run at the next iteration boundary.

When ``BALE_MCP_CONFIG`` points at a server file, the app connects those stdio
servers once at startup and registers their tools on every run.

HTTP runs are non-interactive: write and execute tools are denied unless the
caller sets ``approve_writes=true``.
"""

import asyncio
import hmac
import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from bale_harness.agent.approval import auto_approve
from bale_harness.agent.checkpoint import ShadowGit
from bale_harness.agent.cost import CostRates
from bale_harness.agent.loop import RunResult, build_default_loop
from bale_harness.agent.tools.mcp import load_mcp_config, mcp_tools
from bale_harness.agent.tools.registry import Tool
from bale_harness.agent.trace import (
    JsonlTraceExporter,
    QueueTraceExporter,
    TraceExporter,
)
from bale_harness.contracts import ApprovalMode, Turn
from bale_harness.providers import ModelProvider, create_provider
from bale_harness.repo_index import Bm25Retriever
from bale_harness.settings import Settings
from bale_harness.turns import TurnRequest, build_turn


class RunRequest(BaseModel):
    """Input for executing one turn over HTTP."""

    model_config = ConfigDict(extra="forbid")

    message: str
    workspace: Path
    token_budget: int = Field(default=128_000, gt=0)
    max_iterations: int = Field(default=20, ge=1, le=500)
    approval_mode: ApprovalMode = "approve_write"
    approve_writes: bool = False
    shadow_git: bool = True
    run_id: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$"
    )

    @field_validator("message")
    @classmethod
    def _message_must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("message must not be blank")
        return stripped

    @field_validator("workspace")
    @classmethod
    def _workspace_must_exist(cls, value: Path) -> Path:
        resolved = value.expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError("workspace must be an existing directory")
        return resolved


class RunAlreadyActiveError(Exception):
    """Raised when a caller reuses the id of an in-flight run."""


@dataclass
class RunHandle:
    """Cancellation state shared with one in-flight run."""

    run_id: str
    cancel: asyncio.Event = field(default_factory=asyncio.Event)


class RunRegistry:
    """Track in-flight runs so they can be cancelled by id."""

    def __init__(self) -> None:
        self._runs: dict[str, RunHandle] = {}

    def start(self, run_id: str | None = None) -> RunHandle:
        """Register a run, generating an id when the caller did not supply one."""
        resolved = run_id or uuid4().hex
        if resolved in self._runs:
            raise RunAlreadyActiveError(f"run {resolved} is already active")
        handle = RunHandle(run_id=resolved)
        self._runs[resolved] = handle
        return handle

    def finish(self, handle: RunHandle) -> None:
        self._runs.pop(handle.run_id, None)

    def cancel(self, run_id: str) -> bool:
        handle = self._runs.get(run_id)
        if handle is None:
            return False
        handle.cancel.set()
        return True


class _TeeTraceExporter:
    """Forward trace events to several exporters, ignoring individual failures."""

    def __init__(self, *exporters: TraceExporter) -> None:
        self._exporters = exporters

    async def bind(self, turn: Turn) -> None:
        for exporter in self._exporters:
            try:
                await exporter.bind(turn)
            except Exception:  # tracing is best-effort
                continue

    async def emit(self, event: str, **fields: Any) -> None:
        for exporter in self._exporters:
            try:
                await exporter.emit(event, **fields)
            except Exception:  # tracing is best-effort
                continue


def _cost_rates(settings: Settings) -> CostRates | None:
    if settings.input_cost_per_1k is None or settings.output_cost_per_1k is None:
        return None
    return CostRates(
        input_per_1k=settings.input_cost_per_1k,
        output_per_1k=settings.output_cost_per_1k,
    )


def _build_turn(request: RunRequest) -> Turn:
    return Turn(
        message=request.message,
        workspace=request.workspace,
        session_id=uuid4(),
        token_budget=request.token_budget,
        max_iterations=request.max_iterations,
        approval_mode=request.approval_mode,
    )


async def _execute(
    request: RunRequest,
    settings: Settings,
    provider: ModelProvider,
    *,
    tracer: TraceExporter | None = None,
    cancel: asyncio.Event | None = None,
    extra_tools: Sequence[Tool] | None = None,
) -> RunResult:
    checkpoint = ShadowGit(request.workspace) if request.shadow_git else None
    if tracer is None and settings.trace_path:
        tracer = JsonlTraceExporter(settings.trace_path)
    retriever = (
        Bm25Retriever(request.workspace, top_k=settings.retrieval_top_k)
        if settings.retrieval_enabled
        else None
    )
    loop = build_default_loop(
        provider,
        request.workspace,
        approval_mode=request.approval_mode,
        approver=auto_approve if request.approve_writes else None,
        checkpoint=checkpoint,
        context_window=settings.context_window,
        tracer=tracer,
        cost_rates=_cost_rates(settings),
        extra_tools=extra_tools,
        retriever=retriever,
    )
    try:
        return await loop.run(_build_turn(request), cancelled=cancel)
    finally:
        if checkpoint is not None:
            await checkpoint.cleanup()


def _sse(event: str, data: str) -> str:
    return f"event: {event}\ndata: {data}\n\n"


def _stream_events(
    request: RunRequest,
    settings: Settings,
    provider: ModelProvider,
    handle: RunHandle,
    registry: RunRegistry,
    extra_tools: Sequence[Tool] | None = None,
) -> AsyncIterator[str]:
    """Return an SSE body that runs the turn as events are consumed."""
    queue = QueueTraceExporter()
    tracer: TraceExporter = queue
    if settings.trace_path:
        tracer = _TeeTraceExporter(queue, JsonlTraceExporter(settings.trace_path))

    async def body() -> AsyncIterator[str]:
        task = asyncio.create_task(
            _execute(
                request,
                settings,
                provider,
                tracer=tracer,
                cancel=handle.cancel,
                extra_tools=extra_tools,
            )
        )
        task.add_done_callback(lambda _: queue.close_nowait())
        try:
            yield _sse("run", json.dumps({"run_id": handle.run_id}))
            async for event in queue.stream():
                yield _sse("trace", event.model_dump_json())
            result = await task
            yield _sse("result", result.model_dump_json())
        except asyncio.CancelledError:
            task.cancel()
            raise
        except Exception as error:
            yield _sse("error", json.dumps({"error": str(error)}, ensure_ascii=False))
        finally:
            registry.finish(handle)

    return body()


def create_app(
    settings: Settings | None = None,
    provider: ModelProvider | None = None,
) -> FastAPI:
    """Build the HTTP application, optionally with injected settings/provider."""
    resolved = settings or Settings()
    model_provider = provider or create_provider(resolved)
    api_key = resolved.http_api_key
    mcp_configs = load_mcp_config(resolved.mcp_config) if resolved.mcp_config else []

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        async with mcp_tools(mcp_configs) as tools:
            application.state.extra_tools = tools
            yield

    app = FastAPI(title="Bale Coding Agent Harness", lifespan=lifespan)
    app.state.runs = RunRegistry()
    registry: RunRegistry = app.state.runs

    def _extra_tools() -> Sequence[Tool]:
        return getattr(app.state, "extra_tools", [])

    def _require_auth(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        """Reject requests without the configured bearer token."""
        if api_key is None:
            return
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() == "bearer" and hmac.compare_digest(
            token, api_key.get_secret_value()
        ):
            return
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing API key",
            headers={"WWW-Authenticate": "Bearer"},
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(
        "/v1/turns",
        response_model=Turn,
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(_require_auth)],
    )
    def create_turn(request: TurnRequest) -> Turn:
        return build_turn(request)

    @app.post(
        "/v1/runs",
        response_model=RunResult,
        dependencies=[Depends(_require_auth)],
    )
    async def run_turn(request: RunRequest, response: Response) -> RunResult:
        try:
            handle = registry.start(request.run_id)
        except RunAlreadyActiveError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(error),
            ) from error
        response.headers["X-Run-Id"] = handle.run_id
        try:
            return await _execute(
                request,
                resolved,
                model_provider,
                cancel=handle.cancel,
                extra_tools=_extra_tools(),
            )
        finally:
            registry.finish(handle)

    @app.post(
        "/v1/runs/stream",
        dependencies=[Depends(_require_auth)],
    )
    async def stream_turn(request: RunRequest) -> StreamingResponse:
        try:
            handle = registry.start(request.run_id)
        except RunAlreadyActiveError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(error),
            ) from error
        return StreamingResponse(
            _stream_events(
                request,
                resolved,
                model_provider,
                handle,
                registry,
                extra_tools=_extra_tools(),
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Run-Id": handle.run_id,
            },
        )

    @app.post(
        "/v1/runs/{run_id}/cancel",
        dependencies=[Depends(_require_auth)],
    )
    async def cancel_run(run_id: str) -> dict[str, object]:
        if not registry.cancel(run_id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"unknown run: {run_id}",
            )
        return {"run_id": run_id, "cancelled": True}

    return app
