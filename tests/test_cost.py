import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from bale_harness.agent.cost import CostRates, estimate_cost
from bale_harness.agent.loop import build_default_loop
from bale_harness.agent.messages import ModelResponse, Usage
from bale_harness.contracts import Turn
from bale_harness.providers import FakeProvider


def _turn(tmp_path: Path, **overrides: object) -> Turn:
    values: dict[str, object] = {
        "message": "do work",
        "workspace": tmp_path,
        "token_budget": 10_000,
    }
    values.update(overrides)
    return Turn(**values)


def test_estimate_cost_splits_input_and_output() -> None:
    usage = Usage(input_tokens=1000, output_tokens=500, total_tokens=1500)

    cost = estimate_cost(usage, CostRates(input_per_1k=0.003, output_per_1k=0.006))

    assert cost == pytest.approx(0.006)


def test_estimate_cost_is_zero_for_empty_usage() -> None:
    assert estimate_cost(Usage(), CostRates(input_per_1k=1.0, output_per_1k=2.0)) == 0


def test_rates_reject_negative_values() -> None:
    with pytest.raises(ValidationError):
        CostRates(input_per_1k=-0.1, output_per_1k=0.0)


def test_cost_budget_exceeded_fails_the_turn(tmp_path: Path) -> None:
    provider = FakeProvider(
        [
            ModelResponse(
                content="done",
                usage=Usage(input_tokens=1000, output_tokens=1000, total_tokens=2000),
            )
        ]
    )
    loop = build_default_loop(
        provider,
        tmp_path,
        cost_rates=CostRates(input_per_1k=1.0, output_per_1k=1.0),
    )

    result = asyncio.run(loop.run(_turn(tmp_path, cost_budget=1.0)))

    assert result.status == "failed"
    assert result.error == "cost budget exceeded"
    assert result.estimated_cost == pytest.approx(2.0)


def test_cost_budget_allows_a_run_within_budget(tmp_path: Path) -> None:
    provider = FakeProvider(
        [
            ModelResponse(
                content="done",
                usage=Usage(input_tokens=100, output_tokens=50, total_tokens=150),
            )
        ]
    )
    loop = build_default_loop(
        provider,
        tmp_path,
        cost_rates=CostRates(input_per_1k=1.0, output_per_1k=1.0),
    )

    result = asyncio.run(loop.run(_turn(tmp_path, cost_budget=10.0)))

    assert result.status == "completed"


def test_cost_budget_without_rates_fails_closed(tmp_path: Path) -> None:
    loop = build_default_loop(FakeProvider([ModelResponse(content="done")]), tmp_path)

    result = asyncio.run(loop.run(_turn(tmp_path, cost_budget=1.0)))

    assert result.status == "failed"
    assert result.error == "cost budget requires cost rates"
