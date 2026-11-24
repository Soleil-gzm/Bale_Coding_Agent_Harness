"""Deterministic tokenization for lexical code search.

Identifiers are split on underscores and camelCase boundaries and lowercased,
so ``parseHTTPResponse``, ``parse_http_response``, and ``parseHttpResponse``
all share the tokens ``parse``, ``http``, and ``response``.
"""

import re

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+(?:\.\d+)?")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def split_identifier(token: str) -> list[str]:
    """Split one identifier-like token into lowercase parts."""
    parts: list[str] = []
    for snake_part in token.split("_"):
        if not snake_part:
            continue
        for camel_part in _CAMEL_BOUNDARY.split(snake_part):
            if camel_part:
                parts.append(camel_part.lower())
    return parts


def tokenize(text: str) -> list[str]:
    """Return the ordered BM25 tokens for ``text``."""
    tokens: list[str] = []
    for match in _WORD.finditer(text):
        tokens.extend(split_identifier(match.group()))
    return tokens
