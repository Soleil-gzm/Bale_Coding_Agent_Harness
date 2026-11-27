from pad import pad


def test_pad_keeps_long_text():
    assert pad("abcdef", 3) == "abcdef"
