"""Tax calculation."""

RATE = 0.2


def calc_tax(amount: int, exempt: bool = False) -> float:
    if exempt:
        return 0.0
    return amount * RATE + 1
