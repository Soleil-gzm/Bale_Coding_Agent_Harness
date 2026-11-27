from greet import greet


def test_greeting_contains_name():
    assert "bob" in greet("bob")


def test_greeting_is_string():
    assert isinstance(greet(), str)
