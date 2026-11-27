from pricing import total


def test_total_without_discount():
    assert total(100, 2) == 200
