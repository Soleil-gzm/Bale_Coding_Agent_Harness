"""ReAct agent loop.

The loop drives a turn until the model returns a final answer (no tool calls),
calls the ``finish`` tool successfully, a fatal error occurs, the iteration
budget is exhausted, the token or wall-clock budget is exceeded, or
cancellation is requested. Every tool call goes through the
:class:`ToolRegistry`, so validation, approval, idempotency, timeout, and error
normalization are applied uniformly.

When a :class:`ShadowGit` checkpoint is attached, each iteration is snapshotted
and the final result carries the accumulated diff. When a :class:`SessionStore`
is attached, messages, turn state, executed calls, and after-turn memory are
persisted so an interrupted turn can be resumed without repeating side effects.
When a retriever is attached, its excerpts are injected into the stable system
prefix once per turn as untrusted workspace context.
"""

import asyncio
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from bale_harness.agent.approval import Approver, Decider, PolicyApprover
from bale_harness.agent.checkpoint import ShadowGit, ShadowGitError
from bale_harness.agent.context import (
    DEFAULT_CONTEXT_WINDOW,
    ContextBuilder,
    TokenBudget,
)
from bale_harness.agent.cost import CostRates, estimate_cost
from bale_harness.agent.history import estimate_message_tokens
from bale_harness.agent.memory import curate_memory
from bale_harness.agent.messages import (
    ChatMessage,
    ModelResponse,
    ToolCall,
    ToolSpec,
    Usage,
)
from bale_harness.agent.sandbox import LocalSandbox, Sandbox
from bale_harness.agent.security import WorkspaceGuard
from bale_harness.agent.session import SessionError, SessionStore, TurnRecord
from bale_harness.agent.tools import build_default_tools
from bale_harness.agent.tools.exec import build_exec_tools
from bale_harness.agent.tools.git import build_git_tools
from bale_harness.agent.tools.registry import Tool, ToolRegistry
from bale_harness.agent.tools.write import build_write_tools
from bale_harness.agent.trace import TraceExporter
from bale_harness.contracts import ApprovalMode, Turn
from bale_harness.providers import ModelProvider
from bale_harness.repo_index import Embedder


class Retriever(Protocol):
    """Supplies formatted workspace excerpts relevant to a turn message."""

    def retrieve(self, query: str) -> Sequence[str]:
        """Return the most relevant excerpts, best first."""


class RunResult(BaseModel):
    """Outcome of executing a turn through the agent loop."""

    model_config = ConfigDict(extra="forbid")

    turn_id: UUID
    status: str
    answer: str | None = None
    iterations: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    usage: Usage = Field(default_factory=Usage)
    error: str | None = None
    diff: str | None = None
    checkpoints: list[str] = Field(default_factory=list)
    session_id: UUID | None = None
    resumed: bool = False
    duration_ms: int = Field(default=0, ge=0)
    estimated_cost: float | None = None


def assign_call_ids(calls: list[ToolCall]) -> list[ToolCall]:
    """Ensure every call has a non-empty id, preserving existing ones."""
    for call in calls:
        if not call.call_id:
            call.call_id = f"call-{uuid4().hex[:12]}"
    return calls


def _add_usage(total: Usage, delta: Usage) -> Usage:
    return Usage(
        input_tokens=total.input_tokens + delta.input_tokens,
        output_tokens=total.output_tokens + delta.output_tokens,
        total_tokens=total.total_tokens + delta.total_tokens,
    )


def _prompt_tokens(prompt: list[ChatMessage]) -> int:
    return sum(estimate_message_tokens(message) for message in prompt)


def _duration_ms(start: float) -> int:
    return max(0, int((time.perf_counter() - start) * 1000))


class AgentLoop:
    def __init__(
        self,
        provider: ModelProvider,
        registry: ToolRegistry,
        checkpoint: ShadowGit | None = None,
        session: SessionStore | None = None,
        context_window: int = DEFAULT_CONTEXT_WINDOW,
        tracer: TraceExporter | None = None,
        cost_rates: CostRates | None = None,
        retriever: Retriever | None = None,
    ) -> None:
        self._provider = provider
        self._registry = registry
        self._checkpoint = checkpoint
        self._session = session
        self._context_window = context_window
        self._tracer = tracer
        self._cost_rates = cost_rates
        self._retriever = retriever
        self._run_started: float | None = None

    async def run(
        self,
        turn: Turn,
        cancelled: asyncio.Event | None = None,
        *,
        resume: bool = False,
    ) -> RunResult:
        total_usage = Usage()
        iterations = 0
        tool_calls = 0
        checkpoints: list[str] = []
        history: list[ChatMessage] = []
        resumed = False
        self._run_started = time.perf_counter()

        try:
            await self._start_trace(turn, resumed=resume)
            if self._checkpoint is not None:
                await self._checkpoint.init()
            if self._session is not None:
                await self._session.init()
                await self._session.ensure_session(turn.session_id, turn.workspace)

            if resume:
                turn, history, record = await self._prepare_resume(turn)
                total_usage = record.usage
                iterations = record.iteration
                tool_calls = record.tool_calls
                resumed = True
                if (
                    history
                    and history[-1].role == "assistant"
                    and not history[-1].tool_calls
                ):
                    return await self._completed(
                        turn,
                        history[-1].content,
                        iterations,
                        tool_calls,
                        total_usage,
                        checkpoints,
                        resumed=True,
                    )
            else:
                history = [ChatMessage(role="user", content=turn.message)]
                if self._session is not None:
                    await self._session.start_turn(turn)

            builder = ContextBuilder(
                self._registry.list_specs(),
                memory=await self._load_memory(turn),
                workspace_context=await self._workspace_context(turn),
                budget=TokenBudget(self._context_window),
            )

            for _ in range(turn.max_iterations):
                if cancelled is not None and cancelled.is_set():
                    await self._persist_status(
                        turn,
                        "cancelled",
                        error="cancelled",
                        iterations=iterations,
                        tool_calls=tool_calls,
                        usage=total_usage,
                    )
                    await self._trace_finished(
                        turn,
                        status="cancelled",
                        error="cancelled",
                        iterations=iterations,
                        tool_calls=tool_calls,
                        usage=total_usage,
                    )
                    return RunResult(
                        turn_id=turn.turn_id,
                        status="cancelled",
                        iterations=iterations,
                        tool_calls=tool_calls,
                        usage=total_usage,
                        error="cancelled",
                        checkpoints=checkpoints,
                        session_id=self._session_id(turn),
                        resumed=resumed,
                        duration_ms=self._elapsed_ms(),
                        estimated_cost=self._cost(total_usage),
                    )

                if self._time_exhausted(turn):
                    return await self._failed(
                        turn,
                        "time budget exceeded",
                        iterations,
                        tool_calls,
                        total_usage,
                        checkpoints,
                        resumed,
                    )

                iterations += 1
                specs: list[ToolSpec] = self._registry.list_specs()
                prompt = builder.build(history)
                prompt_tokens = _prompt_tokens(prompt)
                if total_usage.total_tokens + prompt_tokens > turn.token_budget:
                    return await self._failed(
                        turn,
                        "token budget exceeded",
                        iterations,
                        tool_calls,
                        total_usage,
                        checkpoints,
                        resumed,
                    )
                remaining = self._remaining_seconds(turn)
                if remaining is not None and remaining <= 0:
                    return await self._failed(
                        turn,
                        "time budget exceeded",
                        iterations,
                        tool_calls,
                        total_usage,
                        checkpoints,
                        resumed,
                    )
                model_started = time.perf_counter()
                try:
                    response: ModelResponse = await self._complete(
                        prompt, specs, remaining
                    )
                except TimeoutError:
                    return await self._failed(
                        turn,
                        "time budget exceeded",
                        iterations,
                        tool_calls,
                        total_usage,
                        checkpoints,
                        resumed,
                    )
                model_duration = _duration_ms(model_started)
                await self._emit(
                    "model_call",
                    iteration=iterations,
                    duration_ms=model_duration,
                    data={
                        "prompt_tokens": prompt_tokens,
                        "prompt_messages": len(prompt),
                        "usage": response.usage.model_dump(mode="json"),
                        "has_tool_calls": response.has_tool_calls,
                    },
                )
                total_usage = _add_usage(total_usage, response.usage)
                if total_usage.total_tokens > turn.token_budget:
                    return await self._failed(
                        turn,
                        "token budget exceeded",
                        iterations,
                        tool_calls,
                        total_usage,
                        checkpoints,
                        resumed,
                    )

                if not response.has_tool_calls:
                    message = ChatMessage(
                        role="assistant", content=response.content or ""
                    )
                    history.append(message)
                    if self._session is not None:
                        await self._session.record_assistant_message(
                            turn.turn_id, message
                        )
                    return await self._completed(
                        turn,
                        response.content,
                        iterations,
                        tool_calls,
                        total_usage,
                        checkpoints,
                        resumed,
                    )

                calls = assign_call_ids(response.tool_calls)
                assistant = ChatMessage(
                    role="assistant",
                    content=response.content or "",
                    tool_calls=calls,
                )
                history.append(assistant)
                if self._session is not None:
                    await self._session.record_tool_intent(
                        turn.turn_id, assistant, calls
                    )

                results = await self._registry.execute_batch(calls)
                tool_calls += len(calls)
                for call, result in zip(calls, results, strict=True):
                    await self._emit(
                        "tool_call",
                        iteration=iterations,
                        name=call.name,
                        status=result.status,
                        duration_ms=result.latency_ms,
                        data={
                            "call_id": call.call_id,
                            "error_code": result.error_code,
                            "truncated": result.truncated,
                        },
                    )

                if self._checkpoint is not None:
                    snapshot = await self._checkpoint.snapshot(
                        f"bale: iteration {iterations}"
                    )
                    if snapshot is not None:
                        checkpoints.append(snapshot)
                        await self._emit(
                            "checkpoint",
                            iteration=iterations,
                            data={"snapshot": snapshot},
                        )

                tool_messages = [
                    ChatMessage(
                        role="tool",
                        content=result.content,
                        tool_call_id=call.call_id,
                        name=result.name,
                    )
                    for call, result in zip(calls, results, strict=True)
                ]
                if self._session is not None:
                    await self._session.complete_iteration(
                        turn.turn_id,
                        calls,
                        results,
                        tool_messages,
                        iteration=iterations,
                        tool_calls=tool_calls,
                        usage=total_usage,
                    )
                history.extend(tool_messages)

                for call, result in zip(calls, results, strict=True):
                    if result.error_code == "unknown_tool":
                        return await self._failed(
                            turn,
                            f"unknown tool: {call.name}",
                            iterations,
                            tool_calls,
                            total_usage,
                            checkpoints,
                            resumed,
                        )
                    if call.name == "finish" and result.status == "success":
                        return await self._completed(
                            turn,
                            result.content,
                            iterations,
                            tool_calls,
                            total_usage,
                            checkpoints,
                            resumed,
                        )
        except asyncio.CancelledError:
            await self._persist_status(
                turn,
                "cancelled",
                error="cancelled",
                iterations=iterations,
                tool_calls=tool_calls,
                usage=total_usage,
            )
            await self._trace_finished(
                turn,
                status="cancelled",
                error="cancelled",
                iterations=iterations,
                tool_calls=tool_calls,
                usage=total_usage,
            )
            raise
        except Exception as error:  # normalize provider/runtime/session failures
            return await self._failed(
                turn,
                str(error),
                iterations,
                tool_calls,
                total_usage,
                checkpoints,
                resumed,
            )

        return await self._failed(
            turn,
            f"max iterations ({turn.max_iterations}) reached",
            iterations,
            tool_calls,
            total_usage,
            checkpoints,
            resumed,
        )

    async def _prepare_resume(
        self, requested: Turn
    ) -> tuple[Turn, list[ChatMessage], TurnRecord]:
        if self._session is None:
            raise SessionError("resume requires a session store")
        record = await self._session.load_resumable_turn(requested.session_id)
        if record is None:
            raise SessionError(f"no resumable turn for session {requested.session_id}")
        if Path(record.workspace) != Path(requested.workspace):
            raise SessionError("workspace does not match the stored session")

        await self._session.mark_running(record.turn_id)
        history = await self._session.load_messages(record.session_id)
        executed = await self._session.load_executed(record.turn_id)
        self._registry.seed_executed(executed)

        pending = self._missing_results(history)
        if pending:
            synthesized = await self._session.synthesize_unknown_results(
                record.turn_id, pending
            )
            history.extend(synthesized)
        return record.to_turn(), history, record

    def _missing_results(self, history: list[ChatMessage]) -> list[ToolCall]:
        last_index: int | None = None
        for index in range(len(history) - 1, -1, -1):
            message = history[index]
            if message.role == "assistant" and message.tool_calls:
                last_index = index
                break
        if last_index is None:
            return []
        present = {
            message.tool_call_id
            for message in history[last_index + 1 :]
            if message.role == "tool"
        }
        return [
            call
            for call in history[last_index].tool_calls
            if call.call_id not in present
        ]

    async def _load_memory(self, turn: Turn) -> list[str]:
        if self._session is None:
            return []
        return await self._session.load_memory(turn.session_id)

    async def _workspace_context(self, turn: Turn) -> Sequence[str]:
        """Retrieve excerpts off the event loop; failures just skip context."""
        if self._retriever is None:
            return ()
        try:
            return await asyncio.to_thread(self._retriever.retrieve, turn.message)
        except Exception:
            return ()

    def _session_id(self, turn: Turn) -> UUID | None:
        return turn.session_id if self._session is not None else None

    async def _completed(
        self,
        turn: Turn,
        answer: str | None,
        iterations: int,
        tool_calls: int,
        usage: Usage,
        checkpoints: list[str],
        resumed: bool = False,
    ) -> RunResult:
        diff = await self._diff()
        if self._session is not None:
            try:
                await self._session.finish_turn(
                    turn.turn_id,
                    "completed",
                    answer=answer,
                    error=None,
                    iterations=iterations,
                    tool_calls=tool_calls,
                    usage=usage,
                )
            except Exception as error:
                await self._trace_finished(
                    turn,
                    status="failed",
                    error=f"session write failed: {error}",
                    iterations=iterations,
                    tool_calls=tool_calls,
                    usage=usage,
                )
                return RunResult(
                    turn_id=turn.turn_id,
                    status="failed",
                    iterations=iterations,
                    tool_calls=tool_calls,
                    usage=usage,
                    error=f"session write failed: {error}",
                    checkpoints=checkpoints,
                    session_id=self._session_id(turn),
                    resumed=resumed,
                    duration_ms=self._elapsed_ms(),
                    estimated_cost=self._cost(usage),
                )
            await self._store_memory(turn, answer, None)

        await self._trace_finished(
            turn,
            status="completed",
            error=None,
            iterations=iterations,
            tool_calls=tool_calls,
            usage=usage,
        )
        return RunResult(
            turn_id=turn.turn_id,
            status="completed",
            answer=answer,
            iterations=iterations,
            tool_calls=tool_calls,
            usage=usage,
            diff=diff,
            checkpoints=checkpoints,
            session_id=self._session_id(turn),
            resumed=resumed,
            duration_ms=self._elapsed_ms(),
            estimated_cost=self._cost(usage),
        )

    async def _failed(
        self,
        turn: Turn,
        error: str,
        iterations: int,
        tool_calls: int,
        usage: Usage,
        checkpoints: list[str],
        resumed: bool,
    ) -> RunResult:
        await self._persist_status(
            turn,
            "failed",
            error=error,
            iterations=iterations,
            tool_calls=tool_calls,
            usage=usage,
        )
        await self._trace_finished(
            turn,
            status="failed",
            error=error,
            iterations=iterations,
            tool_calls=tool_calls,
            usage=usage,
        )
        return RunResult(
            turn_id=turn.turn_id,
            status="failed",
            iterations=iterations,
            tool_calls=tool_calls,
            usage=usage,
            error=error,
            checkpoints=checkpoints,
            session_id=self._session_id(turn),
            resumed=resumed,
            duration_ms=self._elapsed_ms(),
            estimated_cost=self._cost(usage),
        )

    async def _persist_status(
        self,
        turn: Turn,
        status: str,
        *,
        error: str | None,
        iterations: int,
        tool_calls: int,
        usage: Usage,
    ) -> None:
        if self._session is None:
            return
        try:
            await self._session.finish_turn(
                turn.turn_id,
                status,
                error=error,
                iterations=iterations,
                tool_calls=tool_calls,
                usage=usage,
            )
        except Exception:
            return

    async def _store_memory(
        self, turn: Turn, answer: str | None, error: str | None
    ) -> None:
        if self._session is None:
            return
        entry = curate_memory(turn, status="completed", answer=answer, error=error)
        if entry is None:
            return
        try:
            await self._session.append_memory(turn.session_id, turn.turn_id, entry)
        except Exception:
            return

    async def _start_trace(self, turn: Turn, *, resumed: bool) -> None:
        if self._tracer is None:
            return
        try:
            await self._tracer.bind(turn)
        except Exception:
            self._tracer = None
            return
        await self._emit("turn_started", data={"resumed": resumed})

    async def _emit(self, event: str, **fields: object) -> None:
        if self._tracer is None:
            return
        try:
            await self._tracer.emit(event, **fields)  # type: ignore[arg-type]
        except Exception:
            return

    async def _trace_finished(
        self,
        turn: Turn,
        *,
        status: str,
        error: str | None,
        iterations: int,
        tool_calls: int,
        usage: Usage,
    ) -> None:
        await self._emit(
            "turn_finished",
            status=status,
            iteration=iterations,
            duration_ms=self._elapsed_ms(),
            data={
                "error": error,
                "iterations": iterations,
                "tool_calls": tool_calls,
                "usage": usage.model_dump(mode="json"),
                "estimated_cost": self._cost(usage),
            },
        )

    async def _complete(
        self,
        prompt: list[ChatMessage],
        specs: list[ToolSpec],
        remaining: float | None,
    ) -> ModelResponse:
        """Call the provider, capping the call at the remaining time budget."""
        if remaining is None:
            return await self._provider.complete(prompt, specs)
        return await asyncio.wait_for(
            self._provider.complete(prompt, specs), timeout=remaining
        )

    def _remaining_seconds(self, turn: Turn) -> float | None:
        if turn.time_budget is None or self._run_started is None:
            return None
        return self._run_started + turn.time_budget - time.perf_counter()

    def _time_exhausted(self, turn: Turn) -> bool:
        remaining = self._remaining_seconds(turn)
        return remaining is not None and remaining <= 0

    def _elapsed_ms(self) -> int:
        if self._run_started is None:
            return 0
        return _duration_ms(self._run_started)

    def _cost(self, usage: Usage) -> float | None:
        if self._cost_rates is None:
            return None
        return estimate_cost(usage, self._cost_rates)

    async def _diff(self) -> str | None:
        if self._checkpoint is None:
            return None
        try:
            return await self._checkpoint.diff()
        except ShadowGitError:
            return None


def build_default_loop(
    provider: ModelProvider,
    workspace: Path,
    *,
    approval_mode: ApprovalMode = "approve_write",
    approver: Decider | Approver | None = None,
    sandbox: Sandbox | None = None,
    checkpoint: ShadowGit | None = None,
    session: SessionStore | None = None,
    context_window: int = DEFAULT_CONTEXT_WINDOW,
    tracer: TraceExporter | None = None,
    cost_rates: CostRates | None = None,
    extra_tools: Sequence[Tool] | None = None,
    retriever: Retriever | None = None,
    search_embedder: Embedder | None = None,
    search_alpha: float = 0.5,
    search_index_path: Path | None = None,
) -> AgentLoop:
    """Construct a loop wired to the workspace tools.

    Read tools run freely. Write and execute tools require ``approver`` to
    approve each call; without one they are denied. ``extra_tools`` (for
    example MCP adapters) are registered after the built-ins and follow the
    same governance. ``approver`` is either a synchronous ``Decider`` or an
    ``Approver`` that decides asynchronously (for example over HTTP). A
    ``retriever`` supplies untrusted workspace excerpts for the system prefix
    of each turn. ``search_embedder`` enables hybrid vector ranking for the
    ``search_code`` tool; without it the tool stays lexical.
    """
    guard = WorkspaceGuard(workspace)
    if approver is not None and hasattr(approver, "approve"):
        policy: Approver = approver  # type: ignore[assignment]
    else:
        policy = PolicyApprover(approval_mode, approver)  # type: ignore[arg-type]
    registry = ToolRegistry(policy)
    tools: list[Tool] = [
        *build_default_tools(
            guard,
            search_embedder=search_embedder,
            search_alpha=search_alpha,
            search_index_path=search_index_path,
        ),
        *build_write_tools(guard),
        *build_exec_tools(sandbox or LocalSandbox(guard)),
        *build_git_tools(checkpoint),
    ]
    tools.extend(extra_tools or [])
    for tool in tools:
        registry.register(tool)
    return AgentLoop(
        provider,
        registry,
        checkpoint=checkpoint,
        session=session,
        context_window=context_window,
        tracer=tracer,
        cost_rates=cost_rates,
        retriever=retriever,
    )
