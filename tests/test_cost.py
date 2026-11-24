import pytest
from pydantic import ValidationError

from bale_harness.agent.cost import CostRates, estimate_cost
from bale_harness.agent.messages import Usage


def test_estimate_cost_splits_input_and_output() -> None:
    usage = Usage(input_tokens=1000, output_tokens=500, total_tokens=1500)

    cost = estimate_cost(usage, CostRates(input_per_1k=0.003, output_per_1k=0.006))

    assert cost == pytest.approx(0.006)


def test_estimate_cost_is_zero_for_empty_usage() -> None:
    assert estimate_cost(Usage(), CostRates(input_per_1k=1.0, output_per_1k=2.0)) == 0


def test_rates_reject_negative_values() -> None:
    with pytest.raises(ValidationError):
        CostRates(input_per_1k=-0.1, output_per_1k=0.0)
