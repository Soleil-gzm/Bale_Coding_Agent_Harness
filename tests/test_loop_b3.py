import asyncio
from pathlib import Path
from typing import Any
from uuid import uuid4

from bale_harness.agent.loop import build_default_loop
from bale_harness.agent.messages import (
    ChatMessage,
    ModelResponse,
    ToolCall,
    ToolSpec,
    Usage,
)
from bale_harness.agent.session import SessionError, SessionStore
from bale_harness.contracts import Turn
from bale_harness.providers import FakeProvider


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _turn(
    workspace: Path,
    message: str = "do work",
    *,
    session_id=None,
    turn_id=None,
    token_budget: int = 10_000,
    max_iterations: int = 5,
) -> Turn:
    return Turn(
        message=message,
        workspace=workspace,
        session_id=session_id or uuid4(),
        turn_id=turn_id or uuid4(),
        token_budget=token_budget,
        max_iterations=max_iterations,
    )


class CountingApprover:
    def __init__(self) -> None:
        self.approvals: list[str] = []

    def __call__(self, call: ToolCall, spec: ToolSpec) -> bool:
        self.approvals.append(call.call_id)
        return True


class CancelAfterFirstResponse:
    def __init__(self, event: asyncio.Event) -> None:
        self.event = event
        self.calls: list[tuple[list[ChatMessage], list[ToolSpec]]] = []

    async def complete(
        self, messages: list[ChatMessage], tools: list[ToolSpec]
    ) -> ModelResponse:
        self.calls.append((list(messages), tools))
        self.event.set()
        return ModelResponse(
            tool_calls=[
                ToolCall(
                    call_id="c1",
                    name="write_file",
                    arguments={"path": "out.txt", "content": "one"},
                )
            ],
            usage=Usage(total_tokens=5),
        )


def test_system_prefix_is_stable_within_a_turn(tmp_path: Path) -> None:
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=[ToolCall(call_id="c1", name="list_files", arguments={})]
            ),
            ModelResponse(content="done"),
        ]
    )
    loop = build_default_loop(provider, tmp_path)

    result = _run(loop.run(_turn(tmp_path)))

    assert result.status == "completed"
    first_system = provider.calls[0][0][0]
    second_system = provider.calls[1][0][0]
    assert first_system.role == "system"
    assert first_system.content == second_system.content


def test_token_budget_exceeded_fails_the_turn(tmp_path: Path) -> None:
    provider = FakeProvider(
        [
            ModelResponse(
                content="done",
                usage=Usage(input_tokens=100, output_tokens=10, total_tokens=110),
            )
        ]
    )
    loop = build_default_loop(provider, tmp_path)

    result = _run(loop.run(_turn(tmp_path, token_budget=100)))

    assert result.status == "failed"
    assert result.error == "token budget exceeded"


def test_cancelled_turn_is_persisted_and_resumes_without_duplicate_write(
    tmp_path: Path,
) -> None:
    async def scenario():
        store = SessionStore(tmp_path / "sessions.db")
        approver = CountingApprover()
        turn = _turn(tmp_path, "write out.txt")
        event = asyncio.Event()
        provider = CancelAfterFirstResponse(event)
        loop = build_default_loop(provider, tmp_path, approver=approver, session=store)

        first = await loop.run(turn, cancelled=event)
        first_messages = await store.load_messages(turn.session_id)
        stored = await store.load_turn(turn.turn_id)

        resume_provider = FakeProvider([ModelResponse(content="finished")])
        resume_loop = build_default_loop(resume_provider, tmp_path, session=store)
        second = await resume_loop.run(turn, resume=True)
        second_messages = await store.load_messages(turn.session_id)
        memory = await store.load_memory(turn.session_id)
        return (
            turn,
            approver,
            first,
            first_messages,
            stored,
            resume_provider,
            second,
            second_messages,
            memory,
        )

    (
        turn,
        approver,
        first,
        first_messages,
        stored,
        resume_provider,
        second,
        second_messages,
        memory,
    ) = _run(scenario())

    assert first.status == "cancelled"
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "one"
    assert stored is not None and stored.status == "cancelled"
    assert [message.role for message in first_messages] == [
        "user",
        "assistant",
        "tool",
    ]

    assert second.status == "completed"
    assert second.resumed is True
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "one"
    assert approver.approvals == ["c1"]

    user_messages = [message for message in second_messages if message.role == "user"]
    assert [message.content for message in user_messages] == ["write out.txt"]
    assert (
        len(
            [
                message
                for message in second_messages
                if message.role == "assistant" and message.tool_calls
            ]
        )
        == 1
    )
    assert len(memory) == 1

    resume_prompt = resume_provider.calls[0][0]
    assert [m.content for m in resume_prompt if m.role == "user"] == ["write out.txt"]


def test_resume_only_loads_the_target_turn_messages(tmp_path: Path) -> None:
    async def scenario():
        store = SessionStore(tmp_path / "sessions.db")
        session_id = uuid4()
        cancelled_turn = _turn(tmp_path, "task A", session_id=session_id)
        event = asyncio.Event()
        event.set()
        first_loop = build_default_loop(FakeProvider(), tmp_path, session=store)
        first = await first_loop.run(cancelled_turn, cancelled=event)

        later_turn = _turn(tmp_path, "task B", session_id=session_id)
        later_loop = build_default_loop(
            FakeProvider([ModelResponse(content="answer B")]),
            tmp_path,
            session=store,
        )
        later = await later_loop.run(later_turn)

        resume_provider = FakeProvider([ModelResponse(content="answer A")])
        resume_loop = build_default_loop(resume_provider, tmp_path, session=store)
        resumed = await resume_loop.run(cancelled_turn, resume=True)
        stored = await store.load_turn(cancelled_turn.turn_id)
        return first, later, resume_provider, resumed, stored

    first, later, resume_provider, resumed, stored = _run(scenario())

    assert first.status == "cancelled"
    assert later.status == "completed"
    assert resumed.status == "completed"
    assert resumed.resumed is True
    # The stale turn must answer from its own history, never turn B's answer.
    assert resumed.answer == "answer A"
    assert len(resume_provider.calls) == 1
    resume_prompt = resume_provider.calls[0][0]
    assert [m.content for m in resume_prompt if m.role == "user"] == ["task A"]
    assert stored is not None and stored.status == "completed"
    assert stored.answer == "answer A"


def test_resume_without_session_fails(tmp_path: Path) -> None:
    loop = build_default_loop(FakeProvider(), tmp_path)

    result = _run(loop.run(_turn(tmp_path), resume=True))

    assert result.status == "failed"
    assert result.error is not None
    assert "resume requires a session store" in result.error


def test_session_write_failure_makes_the_turn_fail(tmp_path: Path) -> None:
    class BrokenSession(SessionStore):
        async def start_turn(self, turn: Turn) -> None:
            raise SessionError("disk full")

    store = BrokenSession(tmp_path / "sessions.db")
    loop = build_default_loop(FakeProvider(), tmp_path, session=store)

    result = _run(loop.run(_turn(tmp_path)))

    assert result.status == "failed"
    assert result.error is not None
    assert "disk full" in result.error
