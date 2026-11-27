from slug import slugify


def test_slugify_replaces_spaces():
    assert slugify("hello world") == "hello-world"


def test_slugify_keeps_simple_text():
    assert slugify("abc") == "abc"
