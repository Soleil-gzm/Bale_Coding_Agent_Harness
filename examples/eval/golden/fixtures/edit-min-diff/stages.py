"""Pipeline stages."""

RETRIES = 5


def run_stage(name: str) -> str:
    return f"stage:{name}:{RETRIES}"
