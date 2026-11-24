"""FastAPI entry points for the harness.

``POST /v1/turns`` only constructs a Turn. ``POST /v1/runs`` executes one turn
and returns the final :class:`RunResult`; ``POST /v1/runs/stream`` executes the
same turn and streams trace events as Server-Sent Events, ending with a
``result`` event.

HTTP runs are non-interactive: write and execute tools are denied unless the
caller sets ``approve_writes=true``.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from bale_harness.agent.approval import auto_approve
from bale_harness.agent.checkpoint import ShadowGit
from bale_harness.agent.cost import CostRates
from bale_harness.agent.loop import RunResult, build_default_loop
from bale_harness.agent.trace import (
    JsonlTraceExporter,
    QueueTraceExporter,
    TraceExporter,
)
from bale_harness.contracts import ApprovalMode, Turn
from bale_harness.providers import ModelProvider, create_provider
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
) -> RunResult:
    checkpoint = ShadowGit(request.workspace) if request.shadow_git else None
    if tracer is None and settings.trace_path:
        tracer = JsonlTraceExporter(settings.trace_path)
    loop = build_default_loop(
        provider,
        request.workspace,
        approval_mode=request.approval_mode,
        approver=auto_approve if request.approve_writes else None,
        checkpoint=checkpoint,
        context_window=settings.context_window,
        tracer=tracer,
        cost_rates=_cost_rates(settings),
    )
    try:
        return await loop.run(_build_turn(request))
    finally:
        if checkpoint is not None:
            await checkpoint.cleanup()


def _sse(event: str, data: str) -> str:
    return f"event: {event}\ndata: {data}\n\n"


def _stream_events(
    request: RunRequest, settings: Settings, provider: ModelProvider
) -> AsyncIterator[str]:
    """Return an SSE body that runs the turn as events are consumed."""
    queue = QueueTraceExporter()
    tracer: TraceExporter = queue
    if settings.trace_path:
        tracer = _TeeTraceExporter(queue, JsonlTraceExporter(settings.trace_path))

    async def body() -> AsyncIterator[str]:
        task = asyncio.create_task(_execute(request, settings, provider, tracer=tracer))
        task.add_done_callback(lambda _: queue.close_nowait())
        try:
            async for event in queue.stream():
                yield _sse("trace", event.model_dump_json())
            result = await task
            yield _sse("result", result.model_dump_json())
        except asyncio.CancelledError:
            task.cancel()
            raise
        except Exception as error:
            yield _sse("error", json.dumps({"error": str(error)}, ensure_ascii=False))

    return body()


def create_app(
    settings: Settings | None = None,
    provider: ModelProvider | None = None,
) -> FastAPI:
    """Build the HTTP application, optionally with injected settings/provider."""
    resolved = settings or Settings()
    model_provider = provider or create_provider(resolved)
    app = FastAPI(title="Bale Coding Agent Harness")

    @app.post(
        "/v1/turns",
        response_model=Turn,
        status_code=status.HTTP_201_CREATED,
    )
    def create_turn(request: TurnRequest) -> Turn:
        return build_turn(request)

    @app.post("/v1/runs", response_model=RunResult)
    async def run_turn(request: RunRequest) -> RunResult:
        return await _execute(request, resolved, model_provider)

    @app.post("/v1/runs/stream")
    async def stream_turn(request: RunRequest) -> StreamingResponse:
        return StreamingResponse(
            _stream_events(request, resolved, model_provider),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return app
