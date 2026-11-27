"""Oracle command execution and JUnit parsing for F2P/P2P evaluation.

Runs deterministic acceptance commands (pytest with ``--junitxml``) in an
isolated verification copy and parses the resulting JUnit XML into per-test
statuses. The JUnit ``testcase`` status is authoritative: a zero exit code
with a reported failure is still a failure.
"""

from __future__ import annotations

import os
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from bale_harness.eval.task import OracleSpec

_LOG_CAP_CHARS = 1_000_000
_KEEP_ENV_KEYS = frozenset(
    {
        "PATH",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "HOME",
        "LANG",
        "LC_ALL",
        "SYSTEMDRIVE",
    }
)
_STRIP_ENV_KEYS = frozenset(
    {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    }
)


class OracleError(Exception):
    """Raised when an oracle command cannot be executed or its JUnit parsed."""


@dataclass(frozen=True)
class CommandResult:
    argv: list[str]
    exit_code: int
    duration_ms: int
    timed_out: bool
    log_path: Path
    junit_path: Path | None


@dataclass(frozen=True)
class TestCaseResult:
    node_id: str
    status: Literal["passed", "failed", "skipped"]


@dataclass
class OracleGroupResult:
    """Outcome of one oracle group (f2p / p2p / hidden)."""

    name: str
    status: Literal["passed", "failed", "error"]
    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    missing: int = 0
    cases: list[TestCaseResult] = field(default_factory=list)
    error: str | None = None


@dataclass
class OracleEvaluation:
    """Aggregated outcome of all oracle groups for one task."""

    f2p: OracleGroupResult
    p2p: OracleGroupResult
    hidden: OracleGroupResult

    @property
    def all_passed(self) -> bool:
        return (
            self.f2p.status == "passed"
            and self.p2p.status == "passed"
            and self.hidden.status == "passed"
        )

    @property
    def has_error(self) -> bool:
        return any(g.status == "error" for g in (self.f2p, self.p2p, self.hidden))


def build_eval_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    """Build a sanitized environment for oracle commands.

    Keeps a minimal allowlist of system variables, clears proxy variables so
    tests cannot reach the network accidentally, and pins pytest behaviour.
    """
    source = base_env if base_env is not None else dict(os.environ)
    env = {k: v for k, v in source.items() if k in _KEEP_ENV_KEYS and v is not None}
    for key in _STRIP_ENV_KEYS:
        env.pop(key, None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTEST_ADDOPTS"] = ""
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _is_pytest_command(argv: list[str]) -> bool:
    return any(
        arg == "pytest" or arg.endswith("/pytest") or arg.endswith("\\pytest")
        for arg in argv
    )


def run_command(
    argv: list[str],
    cwd: Path,
    env: dict[str, str],
    timeout_s: float,
    log_path: Path,
    junit_path: Path | None = None,
) -> CommandResult:
    """Run ``argv`` without a shell, capture output, and request JUnit XML."""
    argv = list(argv)
    is_pytest = _is_pytest_command(argv)
    if is_pytest and junit_path is not None:
        # pytest writes the report relative to its own cwd, which is not the
        # harness cwd; pin the report path so parsing finds it afterwards.
        junit_path = Path(junit_path).resolve()
        argv = argv + [f"--junitxml={junit_path}", "-p", "no:cacheprovider"]

    start = time.monotonic()
    timed_out = False
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        exit_code = completed.returncode
        output = (completed.stdout or "") + (completed.stderr or "")
    except subprocess.TimeoutExpired:
        timed_out = True
        exit_code = -1
        output = f"oracle command timed out after {timeout_s}s"
    except OSError as error:
        raise OracleError(
            f"failed to execute oracle command {argv}: {error}"
        ) from error
    duration_ms = int((time.monotonic() - start) * 1000)

    if len(output) > _LOG_CAP_CHARS:
        output = output[:_LOG_CAP_CHARS] + "\n...[truncated]"
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    Path(log_path).write_text(output, encoding="utf-8")

    return CommandResult(
        argv=argv,
        exit_code=exit_code,
        duration_ms=duration_ms,
        timed_out=timed_out,
        log_path=Path(log_path),
        junit_path=Path(junit_path) if junit_path is not None else None,
    )


def parse_junit(path: Path) -> list[TestCaseResult]:
    """Parse a pytest JUnit XML file into per-test statuses.

    Prefers the ``file`` attribute (pytest xunit2) to reconstruct exact node
    ids; falls back to ``classname`` when absent.
    """
    path = Path(path)
    if not path.exists():
        raise OracleError(f"junit xml not found: {path}")
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as error:
        raise OracleError(f"cannot parse junit xml {path}: {error}") from error

    results: list[TestCaseResult] = []
    for case in root.iter("testcase"):
        file_attr = case.get("file")
        name = case.get("name", "")
        if file_attr:
            node_id = f"{file_attr}::{name}"
        else:
            classname = case.get("classname", "")
            node_id = f"{classname}::{name}" if classname else name
        if not node_id:
            continue
        if case.find("failure") is not None or case.find("error") is not None:
            status: Literal["passed", "failed", "skipped"] = "failed"
        elif case.find("skipped") is not None:
            status = "skipped"
        else:
            status = "passed"
        results.append(TestCaseResult(node_id=node_id, status=status))
    return results


def _node_id_matches(node_id: str, expected: set[str]) -> bool:
    return node_id in expected


def evaluate_group(
    commands: list[list[str]],
    expected_ids: set[str] | None,
    verification_dir: Path,
    evidence_dir: Path,
    name: str,
    env: dict[str, str] | None = None,
    timeout_s: float = 60.0,
) -> OracleGroupResult:
    """Run one oracle group's commands and aggregate their JUnit results."""
    run_env = env if env is not None else build_eval_env()
    evidence_dir = Path(evidence_dir).resolve()
    all_cases: list[TestCaseResult] = []
    error_msg: str | None = None

    for index, argv in enumerate(commands):
        log_path = evidence_dir / f"{name}_{index}.log"
        junit_path = evidence_dir / f"{name}_{index}.xml"
        try:
            result = run_command(
                argv, verification_dir, run_env, timeout_s, log_path, junit_path
            )
        except OracleError as error:
            error_msg = str(error)
            break
        if result.timed_out:
            error_msg = f"{name} command timed out"
            break
        try:
            all_cases.extend(parse_junit(junit_path))
        except OracleError as error:
            error_msg = str(error)
            break

    if error_msg is not None:
        return OracleGroupResult(
            name=name, status="error", error=error_msg, cases=all_cases
        )

    actual_ids = {c.node_id for c in all_cases}
    missing = 0
    if expected_ids:
        missing = sum(
            1 for eid in expected_ids if not _node_id_matches(eid, actual_ids)
        )

    passed = sum(1 for c in all_cases if c.status == "passed")
    failed = sum(1 for c in all_cases if c.status == "failed")
    skipped = sum(1 for c in all_cases if c.status == "skipped")
    total = len(all_cases)

    if missing > 0:
        status: Literal["passed", "failed", "error"] = "error"
    elif failed > 0:
        status = "failed"
    else:
        status = "passed"

    return OracleGroupResult(
        name=name,
        status=status,
        total=total,
        passed=passed,
        failed=failed,
        skipped=skipped,
        missing=missing,
        cases=all_cases,
    )


def evaluate_oracle(
    spec: OracleSpec,
    verification_dir: Path,
    evidence_dir: Path,
    env: dict[str, str] | None = None,
    timeout_s: float = 60.0,
) -> OracleEvaluation:
    """Evaluate all oracle groups; each runs even if an earlier group fails."""
    f2p = evaluate_group(
        spec.f2p, None, verification_dir, evidence_dir, "f2p", env, timeout_s
    )
    p2p = evaluate_group(
        spec.p2p, None, verification_dir, evidence_dir, "p2p", env, timeout_s
    )
    hidden = evaluate_group(
        spec.hidden, None, verification_dir, evidence_dir, "hidden", env, timeout_s
    )
    return OracleEvaluation(f2p=f2p, p2p=p2p, hidden=hidden)
