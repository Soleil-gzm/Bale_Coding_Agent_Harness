from truncate import truncate


def test_empty_text():
    assert truncate("", 0) == ""


def test_single_char_at_limit():
    assert truncate("x", 1) == "x"
