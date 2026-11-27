from truncate import truncate


def test_exact_length_keeps_text():
    assert truncate("abcd", 4) == "abcd"


def test_longer_text_keeps_limit():
    assert truncate("abcdef", 3) == "abc"
