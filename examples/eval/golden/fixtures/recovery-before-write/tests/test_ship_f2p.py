from ship import shipping


def test_heavy_shipping_cost():
    assert shipping(10) == 15
