from tax import calc_tax


def test_tax_rate_applies():
    assert calc_tax(100) == 20.0
