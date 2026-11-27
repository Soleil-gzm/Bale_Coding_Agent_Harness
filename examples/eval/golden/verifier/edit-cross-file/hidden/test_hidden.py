from pricing import total


def test_total_with_large_discount():
    assert total(50, 2, 20) == 80
