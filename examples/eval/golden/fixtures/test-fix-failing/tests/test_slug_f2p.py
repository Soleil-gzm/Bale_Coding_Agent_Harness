from slug import slugify


def test_slugify_lowercases():
    assert slugify("Hello World") == "hello-world"
