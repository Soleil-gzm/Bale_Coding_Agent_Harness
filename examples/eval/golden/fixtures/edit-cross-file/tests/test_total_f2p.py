from pricing import total


def test_total_with_discount():
    assert total(100, 2, 10) == 180
