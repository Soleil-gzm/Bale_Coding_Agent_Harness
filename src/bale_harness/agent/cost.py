"""Deterministic cost estimates from token usage.

The runtime never assumes provider pricing. Costs are computed only when the
caller supplies explicit per-1k-token rates, and the result is an estimate
rather than a bill.
"""

from pydantic import BaseModel, ConfigDict, Field

from bale_harness.agent.messages import Usage


class CostRates(BaseModel):
    """Prices per 1000 tokens for input and output."""

    model_config = ConfigDict(extra="forbid")

    input_per_1k: float = Field(ge=0)
    output_per_1k: float = Field(ge=0)


def estimate_cost(usage: Usage, rates: CostRates) -> float:
    """Return the estimated cost of ``usage`` under ``rates``."""
    input_cost = usage.input_tokens * rates.input_per_1k
    output_cost = usage.output_tokens * rates.output_per_1k
    return (input_cost + output_cost) / 1000
