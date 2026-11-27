from truncate import truncate


def test_short_text_is_unchanged():
    assert truncate("ab", 5) == "ab"


def test_shorter_than_limit_is_unchanged():
    assert truncate("abc", 4) == "abc"
