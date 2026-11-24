import asyncio

from bale_harness.agent.approval import (
    PolicyApprover,
    auto_approve,
    deny_all,
)
from bale_harness.agent.messages import ToolCall, ToolSpec

READ_SPEC = ToolSpec(name="read_file", description="read", parameters={})
WRITE_SPEC = ToolSpec(
    name="write_file", description="write", parameters={}, side_effect="write"
)
EXEC_SPEC = ToolSpec(
    name="shell", description="exec", parameters={}, side_effect="execute"
)
CALL = ToolCall(call_id="c1", name="write_file", arguments={})


def _approve(approver: PolicyApprover, spec: ToolSpec) -> bool:
    return asyncio.run(approver.approve(CALL, spec))


def test_auto_read_allows_reads_and_denies_side_effects() -> None:
    approver = PolicyApprover("auto_read", auto_approve)

    assert _approve(approver, READ_SPEC)
    assert not _approve(approver, WRITE_SPEC)
    assert not _approve(approver, EXEC_SPEC)


def test_approve_write_allows_reads_and_gates_side_effects() -> None:
    approver = PolicyApprover("approve_write", auto_approve)

    assert _approve(approver, READ_SPEC)
    assert _approve(approver, WRITE_SPEC)
    assert _approve(approver, EXEC_SPEC)


def test_approve_write_without_decider_denies_side_effects() -> None:
    approver = PolicyApprover("approve_write")

    assert _approve(approver, READ_SPEC)
    assert not _approve(approver, WRITE_SPEC)


def test_approve_all_gates_reads_too() -> None:
    approving = PolicyApprover("approve_all", auto_approve)
    denying = PolicyApprover("approve_all", deny_all)

    assert _approve(approving, READ_SPEC)
    assert _approve(approving, WRITE_SPEC)
    assert not _approve(denying, READ_SPEC)


def test_approve_all_without_decider_denies_everything() -> None:
    approver = PolicyApprover("approve_all")

    assert not _approve(approver, READ_SPEC)


def test_deny_all_denies_side_effects() -> None:
    approver = PolicyApprover("approve_write", deny_all)

    assert not _approve(approver, WRITE_SPEC)
