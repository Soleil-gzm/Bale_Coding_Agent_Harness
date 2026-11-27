"""String truncation helper."""


def truncate(text: str, limit: int) -> str:
    if len(text) >= limit:
        return text[: limit - 1]
    return text
