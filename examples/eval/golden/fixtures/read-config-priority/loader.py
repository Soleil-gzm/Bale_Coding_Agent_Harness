"""Configuration loader with env > file > defaults precedence."""

import json
import os

DEFAULTS = {"timeout": 30, "retries": 3}


def load(name: str, path: str = "config.json") -> int:
    env_value = os.environ.get(name.upper())
    if env_value is not None:
        return int(env_value)
    try:
        with open(path, encoding="utf-8") as handle:
            file_values = json.load(handle)
    except OSError:
        file_values = {}
    if name in file_values:
        return int(file_values[name])
    return DEFAULTS[name]
