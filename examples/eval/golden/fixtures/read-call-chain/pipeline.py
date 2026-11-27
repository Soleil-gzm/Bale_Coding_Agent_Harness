"""Pipeline entry point that delegates each step to ``stages.run_stage``."""

from stages import run_stage


def run(names: list[str]) -> list[str]:
    return [run_stage(name) for name in names]
