"""Approval policies for write and execute tools.

Read tools are safe to run without human involvement. Tools with side effects
are gated by the turn's :data:`~bale_harness.contracts.ApprovalMode`:

- ``auto_read``: reads run automatically; writes and execution are denied.
- ``approve_write``: reads run automatically; writes and execution need a
  decider to approve each call.
- ``approve_all``: every call needs a decider, including reads.

A missing decider always means "deny", so the default behavior is fail-safe.
"""

from collections.abc import Callable
from typing import Protocol

from bale_harness.agent.messages import ToolCall, ToolSpec
from bale_harness.contracts import ApprovalMode

Decider = Callable[[ToolCall, ToolSpec], bool]


def auto_approve(call: ToolCall, spec: ToolSpec) -> bool:
    """Decider that approves every call it is asked about."""
    return True


def deny_all(call: ToolCall, spec: ToolSpec) -> bool:
    """Decider that rejects every call it is asked about."""
    return False


class Approver(Protocol):
    """Decision contract used by the tool registry."""

    async def approve(self, call: ToolCall, spec: ToolSpec) -> bool:
        """Return whether ``call`` may run."""


class PolicyApprover:
    """Apply an approval mode, delegating gated calls to a decider."""

    def __init__(
        self, mode: ApprovalMode = "approve_write", decider: Decider | None = None
    ) -> None:
        self._mode = mode
        self._decider = decider

    async def approve(self, call: ToolCall, spec: ToolSpec) -> bool:
        if self._mode == "auto_read":
            return spec.side_effect == "read"
        if self._mode == "approve_write" and spec.side_effect == "read":
            return True
        if self._decider is None:
            return False
        return bool(self._decider(call, spec))
