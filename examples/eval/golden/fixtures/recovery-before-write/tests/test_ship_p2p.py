from ship import shipping


def test_light_shipping_cost():
    assert shipping(3) == 8
