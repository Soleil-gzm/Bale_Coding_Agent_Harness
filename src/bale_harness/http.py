"""FastAPI entry points for the harness.

``POST /v1/turns`` only constructs a Turn. ``POST /v1/runs`` executes one turn
and returns the final :class:`RunResult`; ``POST /v1/runs/stream`` executes the
same turn and streams trace events as Server-Sent Events, ending with a
``result`` event. ``GET /health`` is public; every ``/v1`` route requires a
bearer token when ``BALE_HTTP_API_KEY`` or ``BALE_HTTP_API_KEYS`` is set.
``POST /v1/runs/{run_id}/cancel`` cooperatively cancels an in-flight run at the
next iteration boundary.

With ``BALE_HTTP_APPROVALS=true``, side-effecting tool calls block until a
caller approves or denies them through
``POST /v1/runs/{run_id}/approvals/{approval_id}``. Pending calls are listed by
``GET /v1/runs/{run_id}/approvals`` and announced as ``approval_requested``
trace events, so an SSE consumer can react without polling. A timeout denies.

With ``BALE_HTTP_SESSION_DB`` set, runs persist to SQLite and share a session
by passing ``session_id``. Two concurrent runs cannot use the same session.

When ``BALE_MCP_CONFIG`` points at a server file, the app connects those stdio
servers once at startup and registers their tools on every run.

HTTP runs are non-interactive: write and execute tools are denied unless the
caller sets ``approve_writes=true`` or answers an approval request.
"""

import asyncio
import json
import math
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from bale_harness.agent.approval import (
    Approver,
    AsyncPolicyApprover,
    Decider,
    auto_approve,
)
from bale_harness.agent.checkpoint import ShadowGit
from bale_harness.agent.cost import CostRates
from bale_harness.agent.loop import RunResult, build_default_loop
from bale_harness.agent.messages import ToolCall, ToolSpec
from bale_harness.agent.sandbox import create_sandbox
from bale_harness.agent.session import SessionStore
from bale_harness.agent.tools.mcp import load_mcp_config, mcp_tools
from bale_harness.agent.tools.registry import Tool
from bale_harness.agent.trace import (
    JsonlTraceExporter,
    QueueTraceExporter,
    TraceExporter,
)
from bale_harness.contracts import ApprovalMode, Turn
from bale_harness.http_support import (
    ApiKeyRing,
    ApprovalBroker,
    ApprovalView,
    AuditLog,
    Identity,
    RateLimiter,
    SessionGate,
)
from bale_harness.providers import ModelProvider, create_provider
from bale_harness.repo_index import create_embedder, create_retriever
from bale_harness.settings import Settings
from bale_harness.turns import TurnRequest, build_turn


class RunRequest(BaseModel):
    """Input for executing one turn over HTTP."""

    model_config = ConfigDict(extra="forbid")

    message: str
    workspace: Path
    token_budget: int = Field(default=128_000, gt=0)
    max_iterations: int = Field(default=20, ge=1, le=500)
    time_budget: float | None = Field(default=None, gt=0)
    approval_mode: ApprovalMode = "approve_write"
    approve_writes: bool = False
    shadow_git: bool = True
    run_id: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$"
    )
    session_id: UUID | None = None

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


class ApprovalDecision(BaseModel):
    """Caller's verdict for one pending tool call."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "deny"]


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


@dataclass
class RunContext:
    """Everything one HTTP run needs after authentication and admission."""

    settings: Settings
    provider: ModelProvider
    run_id: str
    session_id: UUID
    principal: str
    audit: AuditLog
    session_store: SessionStore | None = None
    broker: ApprovalBroker | None = None
    extra_tools: Sequence[Tool] | None = None
    cancel: asyncio.Event | None = None


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


async def _emit_trace(
    tracer: TraceExporter | None,
    event: str,
    *,
    name: str | None = None,
    status: str | None = None,
    data: dict[str, Any] | None = None,
) -> None:
    if tracer is None:
        return
    try:
        await tracer.emit(event, name=name, status=status, data=data)
    except Exception:  # tracing is best-effort
        return


def _resolve_approver(
    request: RunRequest,
    context: RunContext,
    tracer: TraceExporter | None,
) -> Decider | Approver | None:
    """Pick the decider: auto-approve, HTTP broker, or deny by default."""
    if request.approve_writes:
        return auto_approve
    broker = context.broker
    if broker is None:
        return None

    async def decide(call: ToolCall, spec: ToolSpec) -> bool:
        view = broker.open(context.run_id, call, spec)
        await _emit_trace(
            tracer,
            "approval_requested",
            name=spec.name,
            data={
                "approval_id": view.approval_id,
                "side_effect": spec.side_effect,
            },
        )
        await context.audit.record(
            "approval_requested",
            principal=context.principal,
            run_id=context.run_id,
            approval_id=view.approval_id,
            tool=spec.name,
        )
        approved = await broker.wait(view.approval_id)
        await _emit_trace(
            tracer,
            "approval_decided",
            name=spec.name,
            status="approved" if approved else "denied",
            data={"approval_id": view.approval_id},
        )
        await context.audit.record(
            "approval_result",
            principal=context.principal,
            run_id=context.run_id,
            approval_id=view.approval_id,
            tool=spec.name,
            approved=approved,
        )
        return approved

    return AsyncPolicyApprover(request.approval_mode, decide)


async def _execute(
    request: RunRequest,
    context: RunContext,
    *,
    tracer: TraceExporter | None = None,
) -> RunResult:
    settings = context.settings
    checkpoint = ShadowGit(request.workspace) if request.shadow_git else None
    if tracer is None and settings.trace_path:
        tracer = JsonlTraceExporter(settings.trace_path)
    retriever = create_retriever(settings, request.workspace)
    search_embedder = create_embedder(settings)
    sandbox = create_sandbox(
        settings.sandbox_backend,
        request.workspace,
        image=settings.sandbox_image,
        network=settings.sandbox_network,
        memory=settings.sandbox_memory,
        cpus=settings.sandbox_cpus,
        pids_limit=settings.sandbox_pids_limit,
        user=settings.sandbox_user,
    )
    loop = build_default_loop(
        context.provider,
        request.workspace,
        approval_mode=request.approval_mode,
        approver=_resolve_approver(request, context, tracer),
        sandbox=sandbox,
        checkpoint=checkpoint,
        session=context.session_store,
        context_window=settings.context_window,
        tracer=tracer,
        cost_rates=_cost_rates(settings),
        extra_tools=context.extra_tools,
        retriever=retriever,
        search_embedder=search_embedder,
        search_alpha=settings.retrieval_alpha,
        search_index_path=settings.retrieval_index_path,
    )
    turn = Turn(
        message=request.message,
        workspace=request.workspace,
        session_id=context.session_id,
        token_budget=request.token_budget,
        max_iterations=request.max_iterations,
        time_budget=request.time_budget,
        approval_mode=request.approval_mode,
    )
    try:
        return await loop.run(turn, cancelled=context.cancel)
    finally:
        if checkpoint is not None:
            await checkpoint.cleanup()
        if context.broker is not None:
            context.broker.close_run(context.run_id)


def _sse(event: str, data: str) -> str:
    return f"event: {event}\ndata: {data}\n\n"


def _stream_events(
    request: RunRequest,
    context: RunContext,
    handle: RunHandle,
    registry: RunRegistry,
    gate: SessionGate,
) -> AsyncIterator[str]:
    """Return an SSE body that runs the turn as events are consumed."""
    queue = QueueTraceExporter()
    tracer: TraceExporter = queue
    if context.settings.trace_path:
        tracer = _TeeTraceExporter(
            queue, JsonlTraceExporter(context.settings.trace_path)
        )

    async def body() -> AsyncIterator[str]:
        task = asyncio.create_task(_execute(request, context, tracer=tracer))
        task.add_done_callback(lambda _: queue.close_nowait())
        try:
            announcement: dict[str, str] = {"run_id": handle.run_id}
            if context.session_store is not None:
                announcement["session_id"] = str(context.session_id)
            yield _sse("run", json.dumps(announcement))
            async for event in queue.stream():
                yield _sse("trace", event.model_dump_json())
            result = await task
            await context.audit.record(
                "run_finished",
                principal=context.principal,
                run_id=handle.run_id,
                session_id=str(context.session_id),
                status=result.status,
            )
            yield _sse("result", result.model_dump_json())
        except asyncio.CancelledError:
            task.cancel()
            raise
        except Exception as error:
            await context.audit.record(
                "run_failed",
                principal=context.principal,
                run_id=handle.run_id,
                session_id=str(context.session_id),
                error=str(error),
            )
            yield _sse("error", json.dumps({"error": str(error)}, ensure_ascii=False))
        finally:
            gate.release(context.session_id)
            registry.finish(handle)

    return body()


def create_app(
    settings: Settings | None = None,
    provider: ModelProvider | None = None,
) -> FastAPI:
    """Build the HTTP application, optionally with injected settings/provider."""
    resolved = settings or Settings()
    model_provider = provider or create_provider(resolved)
    key_ring = ApiKeyRing.from_settings(resolved)
    limiter = RateLimiter(resolved.http_rate_limit_per_minute)
    session_store = (
        SessionStore(resolved.http_session_db)
        if resolved.http_session_db is not None
        else None
    )
    broker = (
        ApprovalBroker(resolved.http_approval_timeout_seconds)
        if resolved.http_approvals
        else None
    )
    audit = AuditLog(resolved.http_audit_log)
    gate = SessionGate()
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

    def _caller_key(identity: Identity, request: Request) -> str:
        if identity.principal != "anonymous":
            return f"principal:{identity.principal}"
        host = request.client.host if request.client is not None else "unknown"
        return f"ip:{host}"

    async def _authorize(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Identity:
        """Authenticate the caller and apply the per-identity rate limit."""
        identity = key_ring.authenticate(authorization)
        if identity is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid or missing API key",
                headers={"WWW-Authenticate": "Bearer"},
            )
        retry_after = limiter.check(_caller_key(identity, request))
        if retry_after is not None:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="rate limit exceeded",
                headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
            )
        return identity

    def _require_write_scope(identity: Identity, request: RunRequest) -> None:
        if identity.scope == "write":
            return
        if request.approval_mode == "auto_read" and not request.approve_writes:
            return
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="this API key is read-only; use approval_mode=auto_read",
        )

    def _resolve_session_id(request: RunRequest) -> UUID:
        if request.session_id is not None and session_store is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="session_id requires BALE_HTTP_SESSION_DB",
            )
        return request.session_id or uuid4()

    def _admit(request: RunRequest, identity: Identity) -> tuple[RunHandle, UUID]:
        _require_write_scope(identity, request)
        session_id = _resolve_session_id(request)
        try:
            handle = registry.start(request.run_id)
        except RunAlreadyActiveError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(error),
            ) from error
        if not gate.acquire(session_id):
            registry.finish(handle)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"session {session_id} is busy",
            )
        return handle, session_id

    def _context(handle: RunHandle, session_id: UUID, identity: Identity) -> RunContext:
        return RunContext(
            settings=resolved,
            provider=model_provider,
            run_id=handle.run_id,
            session_id=session_id,
            principal=identity.principal,
            audit=audit,
            session_store=session_store,
            broker=broker,
            extra_tools=_extra_tools(),
            cancel=handle.cancel,
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(
        "/v1/turns",
        response_model=Turn,
        status_code=status.HTTP_201_CREATED,
    )
    def create_turn(
        request: TurnRequest,
        identity: Annotated[Identity, Depends(_authorize)],
    ) -> Turn:
        return build_turn(request)

    @app.post("/v1/runs", response_model=RunResult)
    async def run_turn(
        request: RunRequest,
        response: Response,
        identity: Annotated[Identity, Depends(_authorize)],
    ) -> RunResult:
        handle, session_id = _admit(request, identity)
        context = _context(handle, session_id, identity)
        response.headers["X-Run-Id"] = handle.run_id
        response.headers["X-Bale-Principal"] = identity.principal
        if session_store is not None:
            response.headers["X-Session-Id"] = str(session_id)
        await audit.record(
            "run_started",
            principal=identity.principal,
            run_id=handle.run_id,
            session_id=str(session_id),
            workspace=str(request.workspace),
        )
        try:
            result = await _execute(request, context)
            await audit.record(
                "run_finished",
                principal=identity.principal,
                run_id=handle.run_id,
                session_id=str(session_id),
                status=result.status,
            )
            return result
        except Exception as error:
            await audit.record(
                "run_failed",
                principal=identity.principal,
                run_id=handle.run_id,
                session_id=str(session_id),
                error=str(error),
            )
            raise
        finally:
            gate.release(session_id)
            registry.finish(handle)

    @app.post("/v1/runs/stream")
    async def stream_turn(
        request: RunRequest,
        identity: Annotated[Identity, Depends(_authorize)],
    ) -> StreamingResponse:
        handle, session_id = _admit(request, identity)
        context = _context(handle, session_id, identity)
        await audit.record(
            "run_started",
            principal=identity.principal,
            run_id=handle.run_id,
            session_id=str(session_id),
            workspace=str(request.workspace),
        )
        headers = {
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Run-Id": handle.run_id,
            "X-Bale-Principal": identity.principal,
        }
        if session_store is not None:
            headers["X-Session-Id"] = str(session_id)
        return StreamingResponse(
            _stream_events(request, context, handle, registry, gate),
            media_type="text/event-stream",
            headers=headers,
        )

    @app.get(
        "/v1/runs/{run_id}/approvals",
        response_model=list[ApprovalView],
    )
    async def list_approvals(
        run_id: str,
        identity: Annotated[Identity, Depends(_authorize)],
    ) -> list[ApprovalView]:
        if broker is None:
            return []
        return broker.pending(run_id)

    @app.post("/v1/runs/{run_id}/approvals/{approval_id}")
    async def decide_approval(
        run_id: str,
        approval_id: str,
        request: ApprovalDecision,
        identity: Annotated[Identity, Depends(_authorize)],
    ) -> dict[str, object]:
        if identity.scope != "write":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="read-only API keys cannot approve tool calls",
            )
        if broker is None or not broker.resolve(
            run_id, approval_id, request.decision == "approve"
        ):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"unknown approval: {approval_id}",
            )
        await audit.record(
            "approval_decided",
            principal=identity.principal,
            run_id=run_id,
            approval_id=approval_id,
            decision=request.decision,
        )
        return {
            "run_id": run_id,
            "approval_id": approval_id,
            "decision": request.decision,
        }

    @app.post("/v1/runs/{run_id}/cancel")
    async def cancel_run(
        run_id: str,
        identity: Annotated[Identity, Depends(_authorize)],
    ) -> dict[str, object]:
        if not registry.cancel(run_id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"unknown run: {run_id}",
            )
        if broker is not None:
            broker.close_run(run_id)
        await audit.record("run_cancelled", principal=identity.principal, run_id=run_id)
        return {"run_id": run_id, "cancelled": True}

    return app
