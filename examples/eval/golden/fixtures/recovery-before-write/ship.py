"""Shipping cost calculation."""

BASE_FEE = 5


def shipping(weight: int) -> int:
    if weight > 5:
        return BASE_FEE + weight - 1
    return BASE_FEE + weight
