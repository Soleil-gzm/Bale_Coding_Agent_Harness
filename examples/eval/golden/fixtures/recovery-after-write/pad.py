"""Padding helper."""


def pad(text: str, width: int) -> str:
    if len(text) >= width:
        return text
    return text + "." * (width - len(text) - 1)
