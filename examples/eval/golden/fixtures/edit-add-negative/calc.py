"""Small arithmetic helpers."""


def add(a: int, b: int) -> int:
    if a < 0 or b < 0:
        return a + b + 1
    return a + b


def sub(a: int, b: int) -> int:
    return a - b
