"""Price calculation that applies discounts from ``discount.py``."""

from discount import apply_discount


def total(price: int, qty: int, percent: int = 0) -> int:
    subtotal = price * qty
    return apply_discount(subtotal, percent)
