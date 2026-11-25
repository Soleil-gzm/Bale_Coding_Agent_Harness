"""Small, in-process helpers for the production HTTP surface.

The harness serves one trust domain from one process, so these helpers stay
deliberately simple:

- :class:`ApiKeyRing` maps bearer tokens to a principal and a scope, while
  keeping the single ``BALE_HTTP_API_KEY`` deployment working.
- :class:`RateLimiter` applies an in-memory token bucket per caller.
- :class:`ApprovalBroker` holds side-effecting tool calls as futures until a
  second HTTP request approves or denies them.
- :class:`AuditLog` appends one JSON line per run and approval event.
- :class:`SessionGate` rejects two in-flight runs that share one session.

Nothing here survives a restart, which matches the documented single-process
assumption.
"""

import asyncio
import hmac
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict

from bale_harness.agent.messages import ToolCall, ToolSpec
from bale_harness.settings import Settings

Scope = Literal["read", "write"]


@dataclass(frozen=True)
class Identity:
    """Authenticated caller behind one request."""

    principal: str
    scope: Scope = "write"


class ApiKeyRing:
    """Map bearer tokens to identities, preserving the single-key behavior."""

    def __init__(self, entries: dict[str, Identity]) -> None:
        self._entries = entries

    @classmethod
    def from_settings(cls, settings: Settings) -> "ApiKeyRing":
        entries: dict[str, Identity] = {}
        if settings.http_api_key is not None:
            # An empty key still counts as configured so that a blank token
            # cannot silently open the service.
            entries[settings.http_api_key.get_secret_value()] = Identity(
                principal="default", scope="write"
            )
        for key, spec in (settings.http_api_keys or {}).items():
            entries[key] = Identity(principal=spec.principal, scope=spec.scope)
        return cls(entries)

    @property
    def configured(self) -> bool:
        return bool(self._entries)

    def authenticate(self, authorization: str | None) -> Identity | None:
        """Resolve a header to an identity; ``None`` means "reject"."""
        if not self._entries:
            return Identity(principal="anonymous", scope="write")
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            return None
        matched: Identity | None = None
        for key, identity in self._entries.items():
            if hmac.compare_digest(token, key):
                matched = identity
        return matched


class RateLimiter:
    """In-memory token bucket keyed by principal or client address."""

    def __init__(self, per_minute: int) -> None:
        self._capacity = float(per_minute)
        self._rate = per_minute / 60.0
        self._buckets: dict[str, tuple[float, float]] = {}

    @property
    def enabled(self) -> bool:
        return self._capacity > 0

    def check(self, key: str, *, now: float | None = None) -> float | None:
        """Return ``None`` when allowed, else seconds until the caller may retry."""
        if not self.enabled:
            return None
        moment = time.monotonic() if now is None else now
        tokens, updated = self._buckets.get(key, (self._capacity, moment))
        tokens = min(self._capacity, tokens + (moment - updated) * self._rate)
        if tokens < 1.0:
            self._buckets[key] = (tokens, moment)
            return max((1.0 - tokens) / self._rate, 0.001)
        self._buckets[key] = (tokens - 1.0, moment)
        return None


class SessionGate:
    """Refuse concurrent runs that reuse one persisted session."""

    def __init__(self) -> None:
        self._active: set[str] = set()

    def acquire(self, session_id: UUID) -> bool:
        key = str(session_id)
        if key in self._active:
            return False
        self._active.add(key)
        return True

    def release(self, session_id: UUID) -> None:
        self._active.discard(str(session_id))


class AuditLog:
    """Append redacted run and approval records as JSON lines."""

    def __init__(self, path: Path | None) -> None:
        self._path = Path(path).expanduser() if path is not None else None

    @property
    def enabled(self) -> bool:
        return self._path is not None

    async def record(self, event: str, **fields: Any) -> None:
        if self._path is None:
            return
        payload = {"ts": datetime.now(UTC).isoformat(), "event": event, **fields}
        try:
            line = json.dumps(payload, ensure_ascii=False, default=str) + "\n"
        except Exception:  # an unrepresentable record must not break the run
            return
        try:
            await asyncio.to_thread(self._append, line)
        except Exception:  # auditing is best-effort
            return

    def _append(self, line: str) -> None:
        path = self._path
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)


class ApprovalView(BaseModel):
    """What an HTTP caller needs in order to decide one tool call."""

    model_config = ConfigDict(extra="forbid")

    approval_id: str
    run_id: str
    tool: str
    side_effect: str
    arguments: dict[str, Any]
    created_at: str


@dataclass
class _Pending:
    approval_id: str
    run_id: str
    tool: str
    side_effect: str
    arguments: dict[str, Any]
    created_at: str
    future: asyncio.Future[bool]


class ApprovalBroker:
    """Hold side-effecting tool calls until an HTTP caller decides."""

    def __init__(self, timeout_seconds: float = 120.0) -> None:
        self._timeout = timeout_seconds
        self._pending: dict[str, _Pending] = {}

    def open(self, run_id: str, call: ToolCall, spec: ToolSpec) -> ApprovalView:
        """Register one call and return the view announced to the caller."""
        pending = _Pending(
            approval_id=uuid4().hex[:12],
            run_id=run_id,
            tool=spec.name,
            side_effect=spec.side_effect,
            arguments=dict(call.arguments),
            created_at=datetime.now(UTC).isoformat(),
            future=asyncio.get_running_loop().create_future(),
        )
        self._pending[pending.approval_id] = pending
        return _view(pending)

    async def wait(self, approval_id: str) -> bool:
        """Await a decision; a timeout or a closed run denies the call."""
        pending = self._pending.get(approval_id)
        if pending is None:
            return False
        try:
            return await asyncio.wait_for(asyncio.shield(pending.future), self._timeout)
        except TimeoutError:
            self._pending.pop(approval_id, None)
            return False
        except asyncio.CancelledError:
            self._pending.pop(approval_id, None)
            raise

    def resolve(self, run_id: str, approval_id: str, approved: bool) -> bool:
        """Deliver a decision; ``False`` means the approval is unknown."""
        pending = self._pending.get(approval_id)
        if pending is None or pending.run_id != run_id:
            return False
        if not pending.future.done():
            pending.future.set_result(approved)
        self._pending.pop(approval_id, None)
        return True

    def pending(self, run_id: str) -> list[ApprovalView]:
        return [
            _view(pending)
            for pending in self._pending.values()
            if pending.run_id == run_id
        ]

    def close_run(self, run_id: str) -> None:
        """Deny every outstanding approval for a finished or cancelled run."""
        for approval_id, pending in list(self._pending.items()):
            if pending.run_id != run_id:
                continue
            if not pending.future.done():
                pending.future.set_result(False)
            self._pending.pop(approval_id, None)


def _view(pending: _Pending) -> ApprovalView:
    return ApprovalView(
        approval_id=pending.approval_id,
        run_id=pending.run_id,
        tool=pending.tool,
        side_effect=pending.side_effect,
        arguments=pending.arguments,
        created_at=pending.created_at,
    )
