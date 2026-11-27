from greet import greet


def test_greeting_starts_with_hello():
    assert greet("x").startswith("hello")
