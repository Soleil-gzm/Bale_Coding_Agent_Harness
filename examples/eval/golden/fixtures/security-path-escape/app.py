"""Small service used by the path-escape security task."""

NAME = "service"


def health() -> str:
    return "ok"
