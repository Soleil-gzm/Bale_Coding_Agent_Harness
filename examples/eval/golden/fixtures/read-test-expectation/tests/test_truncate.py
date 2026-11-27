from truncate import truncate


def test_truncate_keeps_limit():
    assert truncate("abcdef", 3) == "abc"


def test_truncate_short_text_is_unchanged():
    assert truncate("ab", 5) == "ab"
