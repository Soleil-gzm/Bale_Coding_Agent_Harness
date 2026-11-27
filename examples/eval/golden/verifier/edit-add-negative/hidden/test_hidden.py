from calc import add


def test_add_large_negatives():
    assert add(-100, -200) == -300


def test_add_negative_zero():
    assert add(-1, 0) == -1
