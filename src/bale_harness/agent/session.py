"""SQLite session persistence for turns, messages, executed calls, and memory.

Every operation uses a short-lived connection created inside the worker thread.
A transaction's statements never cross an ``await`` boundary, which keeps the
atomicity guarantee valid on Windows as well as POSIX.
"""

import asyncio
import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from bale_harness.agent.messages import (
    CachedResult,
    ChatMessage,
    ToolCall,
    Usage,
    argument_fingerprint,
)
from bale_harness.contracts import ApprovalMode, ToolResult, Turn, TurnStatus

TERMINAL_STATUSES = {"completed", "failed"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    workspace TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS turns (
    turn_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    message TEXT NOT NULL,
    workspace TEXT NOT NULL,
    repo_revision TEXT,
    token_budget INTEGER NOT NULL,
    cost_budget REAL,
    max_iterations INTEGER NOT NULL,
    approval_mode TEXT NOT NULL,
    status TEXT NOT NULL,
    iteration INTEGER NOT NULL DEFAULT 0,
    tool_calls INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    answer TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    tool_call_id TEXT,
    name TEXT,
    tool_calls_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    UNIQUE(session_id, seq)
);

CREATE TABLE IF NOT EXISTS executed_calls (
    turn_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    name TEXT NOT NULL,
    args_hash TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    status TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    error_code TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    latency_ms INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (turn_id, call_id, args_hash)
);

CREATE TABLE IF NOT EXISTS memory_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    turn_id TEXT,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);
"""


class SessionError(Exception):
    """Raised when session state is missing or inconsistent."""


class TurnRecord(BaseModel):
    """A turn as persisted in the session database."""

    model_config = ConfigDict(extra="forbid")

    turn_id: UUID
    session_id: UUID
    message: str
    workspace: Path
    repo_revision: str | None = None
    token_budget: int
    cost_budget: float | None = None
    max_iterations: int
    approval_mode: ApprovalMode
    status: TurnStatus
    iteration: int = 0
    tool_calls: int = 0
    usage: Usage = Usage()
    answer: str | None = None
    error: str | None = None

    def to_turn(self) -> Turn:
        """Rebuild a runnable Turn; the run continues in ``running`` state."""
        return Turn(
            turn_id=self.turn_id,
            session_id=self.session_id,
            message=self.message,
            workspace=self.workspace,
            repo_revision=self.repo_revision,
            token_budget=self.token_budget,
            cost_budget=self.cost_budget,
            max_iterations=self.max_iterations,
            approval_mode=self.approval_mode,
            status="running",
        )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _workspace_key(workspace: Path) -> str:
    return str(Path(workspace).expanduser().resolve())


class SessionStore:
    """Append-only session state backed by SQLite."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()
        self._initialized = False

    @property
    def db_path(self) -> Path:
        return self._db_path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def _transaction(self):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()

    def _init_sync(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)
        finally:
            connection.close()

    async def init(self) -> None:
        if self._initialized:
            return
        await asyncio.to_thread(self._init_sync)
        self._initialized = True

    async def ensure_session(self, session_id: UUID, workspace: Path) -> None:
        await self.init()
        await asyncio.to_thread(self._ensure_session_sync, session_id, workspace)

    def _ensure_session_sync(self, session_id: UUID, workspace: Path) -> None:
        with self._transaction() as connection:
            self._ensure_session_row(connection, session_id, workspace)

    def _ensure_session_row(
        self, connection: sqlite3.Connection, session_id: UUID, workspace: Path
    ) -> None:
        key = _workspace_key(workspace)
        row = connection.execute(
            "SELECT workspace FROM sessions WHERE session_id = ?", (str(session_id),)
        ).fetchone()
        now = _now()
        if row is None:
            connection.execute(
                "INSERT INTO sessions (session_id, workspace, created_at, updated_at)"
                " VALUES (?, ?, ?, ?)",
                (str(session_id), key, now, now),
            )
        elif Path(row["workspace"]) != Path(key):
            raise SessionError(
                f"session {session_id} belongs to workspace {row['workspace']}"
            )
        else:
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                (now, str(session_id)),
            )

    async def start_turn(self, turn: Turn) -> None:
        await self.init()
        await asyncio.to_thread(self._start_turn_sync, turn)

    def _start_turn_sync(self, turn: Turn) -> None:
        with self._transaction() as connection:
            self._ensure_session_row(connection, turn.session_id, turn.workspace)
            exists = connection.execute(
                "SELECT 1 FROM turns WHERE turn_id = ?", (str(turn.turn_id),)
            ).fetchone()
            if exists is not None:
                return
            now = _now()
            connection.execute(
                "INSERT INTO turns (turn_id, session_id, message, workspace,"
                " repo_revision, token_budget, cost_budget, max_iterations,"
                " approval_mode, status, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(turn.turn_id),
                    str(turn.session_id),
                    turn.message,
                    _workspace_key(turn.workspace),
                    turn.repo_revision,
                    turn.token_budget,
                    turn.cost_budget,
                    turn.max_iterations,
                    turn.approval_mode,
                    "running",
                    now,
                    now,
                ),
            )
            self._insert_message(
                connection,
                turn.session_id,
                turn.turn_id,
                ChatMessage(role="user", content=turn.message),
            )

    def _insert_message(
        self,
        connection: sqlite3.Connection,
        session_id: UUID,
        turn_id: UUID,
        message: ChatMessage,
    ) -> None:
        row = connection.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM messages"
            " WHERE session_id = ?",
            (str(session_id),),
        ).fetchone()
        connection.execute(
            "INSERT INTO messages (session_id, turn_id, seq, role, content,"
            " tool_call_id, name, tool_calls_json, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(session_id),
                str(turn_id),
                row["next"],
                message.role,
                message.content,
                message.tool_call_id,
                message.name,
                json.dumps(
                    [call.model_dump() for call in message.tool_calls],
                    separators=(",", ":"),
                ),
                _now(),
            ),
        )

    def _session_for_turn(self, connection: sqlite3.Connection, turn_id: UUID) -> UUID:
        row = connection.execute(
            "SELECT session_id FROM turns WHERE turn_id = ?", (str(turn_id),)
        ).fetchone()
        if row is None:
            raise SessionError(f"unknown turn: {turn_id}")
        return UUID(row["session_id"])

    async def record_tool_intent(
        self, turn_id: UUID, assistant_message: ChatMessage, calls: list[ToolCall]
    ) -> None:
        await self.init()
        await asyncio.to_thread(
            self._record_tool_intent_sync, turn_id, assistant_message, calls
        )

    def _record_tool_intent_sync(
        self, turn_id: UUID, assistant_message: ChatMessage, calls: list[ToolCall]
    ) -> None:
        with self._transaction() as connection:
            session_id = self._session_for_turn(connection, turn_id)
            self._insert_message(connection, session_id, turn_id, assistant_message)
            now = _now()
            for call in calls:
                fingerprint = argument_fingerprint(call.arguments)
                connection.execute(
                    "INSERT OR REPLACE INTO executed_calls (turn_id, call_id, name,"
                    " args_hash, arguments_json, status, content, error_code,"
                    " metadata_json, latency_ms, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(turn_id),
                        call.call_id,
                        call.name,
                        fingerprint,
                        json.dumps(call.arguments, sort_keys=True, default=str),
                        "pending",
                        "",
                        None,
                        "{}",
                        0,
                        now,
                    ),
                )

    async def record_assistant_message(
        self, turn_id: UUID, message: ChatMessage
    ) -> None:
        await self.init()
        await asyncio.to_thread(self._record_assistant_message_sync, turn_id, message)

    def _record_assistant_message_sync(
        self, turn_id: UUID, message: ChatMessage
    ) -> None:
        with self._transaction() as connection:
            session_id = self._session_for_turn(connection, turn_id)
            self._insert_message(connection, session_id, turn_id, message)

    async def complete_iteration(
        self,
        turn_id: UUID,
        calls: list[ToolCall],
        results: list[ToolResult],
        tool_messages: list[ChatMessage],
        *,
        iteration: int,
        tool_calls: int,
        usage: Usage,
    ) -> None:
        await self.init()
        await asyncio.to_thread(
            self._complete_iteration_sync,
            turn_id,
            calls,
            results,
            tool_messages,
            iteration,
            tool_calls,
            usage,
        )

    def _complete_iteration_sync(
        self,
        turn_id: UUID,
        calls: list[ToolCall],
        results: list[ToolResult],
        tool_messages: list[ChatMessage],
        iteration: int,
        tool_calls: int,
        usage: Usage,
    ) -> None:
        if not (len(calls) == len(results) == len(tool_messages)):
            raise SessionError("calls, results, and tool messages must align")
        with self._transaction() as connection:
            session_id = self._session_for_turn(connection, turn_id)
            now = _now()
            for call, result in zip(calls, results, strict=True):
                fingerprint = argument_fingerprint(call.arguments)
                cursor = connection.execute(
                    "UPDATE executed_calls SET status = ?, content = ?,"
                    " error_code = ?, metadata_json = ?, latency_ms = ?,"
                    " updated_at = ? WHERE turn_id = ? AND call_id = ?"
                    " AND args_hash = ?",
                    (
                        result.status,
                        result.content,
                        result.error_code,
                        json.dumps(result.metadata, sort_keys=True, default=str),
                        result.latency_ms,
                        now,
                        str(turn_id),
                        call.call_id,
                        fingerprint,
                    ),
                )
                if cursor.rowcount == 0:
                    raise SessionError(f"missing tool intent for call {call.call_id}")
            for message in tool_messages:
                self._insert_message(connection, session_id, turn_id, message)
            connection.execute(
                "UPDATE turns SET iteration = ?, tool_calls = ?, input_tokens = ?,"
                " output_tokens = ?, total_tokens = ?, updated_at = ?"
                " WHERE turn_id = ?",
                (
                    iteration,
                    tool_calls,
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.total_tokens,
                    now,
                    str(turn_id),
                ),
            )

    async def synthesize_unknown_results(
        self, turn_id: UUID, calls: list[ToolCall]
    ) -> list[ChatMessage]:
        await self.init()
        return await asyncio.to_thread(
            self._synthesize_unknown_results_sync, turn_id, calls
        )

    def _synthesize_unknown_results_sync(
        self, turn_id: UUID, calls: list[ToolCall]
    ) -> list[ChatMessage]:
        messages: list[ChatMessage] = []
        with self._transaction() as connection:
            session_id = self._session_for_turn(connection, turn_id)
            now = _now()
            for call in calls:
                fingerprint = argument_fingerprint(call.arguments)
                content = (
                    f"Previous call to {call.name} was interrupted; its outcome"
                    " is unknown and it was not re-executed."
                )
                cursor = connection.execute(
                    "UPDATE executed_calls SET status = 'unknown', content = ?,"
                    " error_code = 'outcome_unknown', updated_at = ?"
                    " WHERE turn_id = ? AND call_id = ? AND args_hash = ?",
                    (content, now, str(turn_id), call.call_id, fingerprint),
                )
                if cursor.rowcount == 0:
                    connection.execute(
                        "INSERT INTO executed_calls (turn_id, call_id, name,"
                        " args_hash, arguments_json, status, content, error_code,"
                        " metadata_json, latency_ms, updated_at)"
                        " VALUES (?, ?, ?, ?, ?, 'unknown', ?, 'outcome_unknown',"
                        " '{}', 0, ?)",
                        (
                            str(turn_id),
                            call.call_id,
                            call.name,
                            fingerprint,
                            json.dumps(call.arguments, sort_keys=True, default=str),
                            content,
                            now,
                        ),
                    )
                message = ChatMessage(
                    role="tool",
                    content=content,
                    tool_call_id=call.call_id,
                    name=call.name,
                )
                self._insert_message(connection, session_id, turn_id, message)
                messages.append(message)
        return messages

    async def mark_running(self, turn_id: UUID) -> None:
        await self.init()
        await asyncio.to_thread(self._mark_running_sync, turn_id)

    def _mark_running_sync(self, turn_id: UUID) -> None:
        with self._transaction() as connection:
            connection.execute(
                "UPDATE turns SET status = 'running', updated_at = ?"
                " WHERE turn_id = ? AND status IN ('running', 'cancelled')",
                (_now(), str(turn_id)),
            )

    async def finish_turn(
        self,
        turn_id: UUID,
        status: TurnStatus,
        *,
        answer: str | None = None,
        error: str | None = None,
        iterations: int = 0,
        tool_calls: int = 0,
        usage: Usage | None = None,
    ) -> None:
        await self.init()
        await asyncio.to_thread(
            self._finish_turn_sync,
            turn_id,
            status,
            answer,
            error,
            iterations,
            tool_calls,
            usage or Usage(),
        )

    def _finish_turn_sync(
        self,
        turn_id: UUID,
        status: TurnStatus,
        answer: str | None,
        error: str | None,
        iterations: int,
        tool_calls: int,
        usage: Usage,
    ) -> None:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT status FROM turns WHERE turn_id = ?", (str(turn_id),)
            ).fetchone()
            if row is None:
                raise SessionError(f"unknown turn: {turn_id}")
            if row["status"] in TERMINAL_STATUSES:
                return
            connection.execute(
                "UPDATE turns SET status = ?, answer = ?, error = ?,"
                " iteration = ?, tool_calls = ?, input_tokens = ?,"
                " output_tokens = ?, total_tokens = ?, updated_at = ?"
                " WHERE turn_id = ?",
                (
                    status,
                    answer,
                    error,
                    iterations,
                    tool_calls,
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.total_tokens,
                    _now(),
                    str(turn_id),
                ),
            )

    async def load_messages(self, session_id: UUID) -> list[ChatMessage]:
        await self.init()
        return await asyncio.to_thread(self._load_messages_sync, session_id)

    def _load_messages_sync(self, session_id: UUID) -> list[ChatMessage]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT role, content, tool_call_id, name, tool_calls_json"
                " FROM messages WHERE session_id = ? ORDER BY seq",
                (str(session_id),),
            ).fetchall()
        finally:
            connection.close()
        return [
            ChatMessage(
                role=row["role"],
                content=row["content"],
                tool_call_id=row["tool_call_id"],
                name=row["name"],
                tool_calls=json.loads(row["tool_calls_json"] or "[]"),
            )
            for row in rows
        ]

    async def load_executed(self, turn_id: UUID) -> list[CachedResult]:
        await self.init()
        return await asyncio.to_thread(self._load_executed_sync, turn_id)

    def _load_executed_sync(self, turn_id: UUID) -> list[CachedResult]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT call_id, name, args_hash, status, content, error_code,"
                " metadata_json, latency_ms FROM executed_calls"
                " WHERE turn_id = ? AND status != 'pending'",
                (str(turn_id),),
            ).fetchall()
        finally:
            connection.close()
        cached: list[CachedResult] = []
        for row in rows:
            status = row["status"]
            error_code = row["error_code"]
            if status == "unknown":
                status = "error"
                error_code = error_code or "outcome_unknown"
            cached.append(
                CachedResult(
                    call_id=row["call_id"],
                    name=row["name"],
                    args_hash=row["args_hash"],
                    result=ToolResult(
                        call_id=row["call_id"],
                        name=row["name"],
                        status=status,
                        content=row["content"],
                        error_code=error_code,
                        latency_ms=row["latency_ms"],
                        metadata=json.loads(row["metadata_json"] or "{}"),
                    ),
                )
            )
        return cached

    async def load_resumable_turn(self, session_id: UUID) -> TurnRecord | None:
        await self.init()
        return await asyncio.to_thread(self._load_resumable_turn_sync, session_id)

    def _load_resumable_turn_sync(self, session_id: UUID) -> TurnRecord | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM turns WHERE session_id = ?"
                " AND status IN ('running', 'cancelled')"
                " ORDER BY rowid DESC LIMIT 1",
                (str(session_id),),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return _turn_record(row)

    async def load_turn(self, turn_id: UUID) -> TurnRecord | None:
        await self.init()
        return await asyncio.to_thread(self._load_turn_sync, turn_id)

    def _load_turn_sync(self, turn_id: UUID) -> TurnRecord | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM turns WHERE turn_id = ?", (str(turn_id),)
            ).fetchone()
        finally:
            connection.close()
        return None if row is None else _turn_record(row)

    async def list_turns(self, session_id: UUID) -> list[TurnRecord]:
        await self.init()
        return await asyncio.to_thread(self._list_turns_sync, session_id)

    def _list_turns_sync(self, session_id: UUID) -> list[TurnRecord]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM turns WHERE session_id = ? ORDER BY rowid",
                (str(session_id),),
            ).fetchall()
        finally:
            connection.close()
        return [_turn_record(row) for row in rows]

    async def append_memory(
        self, session_id: UUID, turn_id: UUID, content: str
    ) -> None:
        await self.init()
        await asyncio.to_thread(self._append_memory_sync, session_id, turn_id, content)

    def _append_memory_sync(
        self, session_id: UUID, turn_id: UUID, content: str
    ) -> None:
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO memory_entries (session_id, turn_id, content,"
                " created_at) VALUES (?, ?, ?, ?)",
                (str(session_id), str(turn_id), content, _now()),
            )

    async def load_memory(self, session_id: UUID, limit: int = 5) -> list[str]:
        await self.init()
        return await asyncio.to_thread(self._load_memory_sync, session_id, limit)

    def _load_memory_sync(self, session_id: UUID, limit: int) -> list[str]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT content FROM memory_entries WHERE session_id = ?"
                " ORDER BY id DESC LIMIT ?",
                (str(session_id), max(0, limit)),
            ).fetchall()
        finally:
            connection.close()
        return [row["content"] for row in reversed(rows)]


def _turn_record(row: sqlite3.Row) -> TurnRecord:
    return TurnRecord(
        turn_id=UUID(row["turn_id"]),
        session_id=UUID(row["session_id"]),
        message=row["message"],
        workspace=Path(row["workspace"]),
        repo_revision=row["repo_revision"],
        token_budget=row["token_budget"],
        cost_budget=row["cost_budget"],
        max_iterations=row["max_iterations"],
        approval_mode=row["approval_mode"],
        status=row["status"],
        iteration=row["iteration"],
        tool_calls=row["tool_calls"],
        usage=Usage(
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            total_tokens=row["total_tokens"],
        ),
        answer=row["answer"],
        error=row["error"],
    )
