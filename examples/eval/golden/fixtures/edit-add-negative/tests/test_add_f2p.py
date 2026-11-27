from calc import add


def test_add_negatives():
    assert add(-2, -3) == -5


def test_add_mixed_sign():
    assert add(-2, 3) == 1
