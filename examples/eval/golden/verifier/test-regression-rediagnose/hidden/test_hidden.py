from tax import calc_tax


def test_tax_on_zero_amount():
    assert calc_tax(0) == 0.0


def test_tax_on_small_amount():
    assert calc_tax(50) == 10.0
