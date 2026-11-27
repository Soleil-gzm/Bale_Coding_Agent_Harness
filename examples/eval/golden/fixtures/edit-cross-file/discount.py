"""Discount rules."""


def apply_discount(amount: int, percent: int) -> int:
    return amount - amount * percent // 10
