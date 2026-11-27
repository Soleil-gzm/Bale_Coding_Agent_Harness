from tax import calc_tax


def test_exempt_amount_has_no_tax():
    assert calc_tax(100, exempt=True) == 0.0


def test_tax_is_float():
    assert isinstance(calc_tax(10), float)
