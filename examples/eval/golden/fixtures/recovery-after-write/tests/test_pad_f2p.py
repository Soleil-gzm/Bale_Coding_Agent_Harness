from pad import pad


def test_pad_reaches_width():
    assert pad("ab", 5) == "ab..."
