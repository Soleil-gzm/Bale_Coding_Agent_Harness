"""Controlled interruption injection for recovery evaluation scenarios.

An :class:`InterruptController` implements the runtime
:class:`~bale_harness.agent.tools.registry.InterruptHook` protocol and flips a
``cancelled`` event at a precise execution point chosen by
:class:`InterruptSpec`. This lets the evaluation harness deterministically
reproduce:

* a run cancelled after N completed iterations,
* the K-th side-effect tool blocked before it executes (write-before crash),
* the K-th side-effect tool executed but not yet persisted (result-unknown).

The controller only observes and signals; the runtime loop owns the actual
cancellation handling and persistence decisions.
"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel, ConfigDict, Field, model_validator

from bale_harness.agent.messages import ToolCall
from bale_harness.agent.tools.registry import InterruptHook
from bale_harness.contracts import ToolResult


class InterruptSpec(BaseModel):
    """Configuration for where an injected run should be interrupted.

    At least one trigger must be set. All thresholds are 1-based.
    """

    model_config = ConfigDict(extra="forbid")

    after_iterations: int | None = Field(default=None, ge=1)
    before_side_effect: int | None = Field(default=None, ge=1)
    after_side_effect: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _at_least_one_trigger(self) -> InterruptSpec:
        if all(
            value is None
            for value in (
                self.after_iterations,
                self.before_side_effect,
                self.after_side_effect,
            )
        ):
            raise ValueError("at least one interrupt trigger must be set")
        return self


class InterruptController(InterruptHook):
    """Count execution events and flip ``cancelled`` at the configured point."""

    def __init__(self, spec: InterruptSpec) -> None:
        self._spec = spec
        self.cancelled: asyncio.Event = asyncio.Event()
        self._iterations = 0
        self._side_effects = 0

    @property
    def iterations_seen(self) -> int:
        return self._iterations

    @property
    def side_effects_seen(self) -> int:
        return self._side_effects

    async def on_iteration_start(self) -> None:
        self._iterations += 1
        if (
            self._spec.after_iterations is not None
            and self._iterations > self._spec.after_iterations
        ):
            self.cancelled.set()

    async def before_side_effect(self, call: ToolCall) -> bool:
        self._side_effects += 1
        if (
            self._spec.before_side_effect is not None
            and self._side_effects == self._spec.before_side_effect
        ):
            self.cancelled.set()
            return True
        return False

    async def after_side_effect(self, call: ToolCall, result: ToolResult) -> None:
        if (
            self._spec.after_side_effect is not None
            and self._side_effects == self._spec.after_side_effect
        ):
            self.cancelled.set()
