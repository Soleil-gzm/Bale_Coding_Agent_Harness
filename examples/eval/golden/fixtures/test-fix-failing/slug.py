"""Slug helper."""


def slugify(text: str) -> str:
    return text.replace(" ", "-")
