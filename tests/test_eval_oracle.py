"""Tests for the oracle command executor and JUnit parser (E1 commit 2)."""

import sys
from pathlib import Path

import pytest

from bale_harness.eval import OracleError, OracleSpec, evaluate_oracle, parse_junit
from bale_harness.eval.oracle import build_eval_env, evaluate_group, run_command

PY = [sys.executable, "-m", "pytest", "-q"]
BUGGY = "def total(price, qty):\n    return price * qty\n"
FIXED = "def total(price, qty, discount=0):\n    return price * qty - discount\n"
TESTS = (
    "import pricing\n\n"
    "def test_f2p_discount():\n"
    "    assert pricing.total(100, 2, discount=10) == 190\n\n"
    "def test_p2p_basic():\n"
    "    assert pricing.total(10, 3) == 30\n"
)
F2P_NODE = "tests/test_pricing.py::test_f2p_discount"
P2P_NODE = "tests/test_pricing.py::test_p2p_basic"


def _write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_workspace(tmp_path: Path, fixed: bool = False) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    _write(ws, "pricing.py", FIXED if fixed else BUGGY)
    _write(ws, "tests/test_pricing.py", TESTS)
    return ws


def _spec() -> OracleSpec:
    return OracleSpec(
        f2p=[PY + [F2P_NODE]],
        p2p=[PY + [P2P_NODE]],
        hidden=[],
    )


def test_pristine_f2p_fails_p2p_passes(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)
    evidence = tmp_path / "evidence"

    result = evaluate_oracle(_spec(), ws, evidence, env=build_eval_env(), timeout_s=60)

    assert result.f2p.status == "failed"
    assert result.p2p.status == "passed"
    assert result.hidden.status == "passed"
    assert not result.all_passed


def test_fixed_workspace_all_pass(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path, fixed=True)
    evidence = tmp_path / "evidence"

    result = evaluate_oracle(_spec(), ws, evidence, env=build_eval_env(), timeout_s=60)

    assert result.f2p.status == "passed"
    assert result.p2p.status == "passed"
    assert result.all_passed


def test_missing_expected_node_is_error(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path, fixed=True)
    evidence = tmp_path / "evidence"

    group = evaluate_group(
        [PY + [P2P_NODE]],
        expected_ids={"does/not/exist.py::test_missing"},
        verification_dir=ws,
        evidence_dir=evidence,
        name="p2p",
        env=build_eval_env(),
        timeout_s=60,
    )

    assert group.status == "error"
    assert group.missing == 1


def test_parse_junit_failure_is_failed_even_without_exit_code(tmp_path: Path) -> None:
    xml = tmp_path / "result.xml"
    xml.write_text(
        '<?xml version="1.0"?>'
        "<testsuites><testsuite>"
        '<testcase classname="tests.test_x" name="test_a" '
        'file="tests/test_x.py" line="1">'
        "<failure>boom</failure></testcase>"
        '<testcase classname="tests.test_x" name="test_b" '
        'file="tests/test_x.py" line="5"/>'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )

    cases = parse_junit(xml)

    by_id = {c.node_id: c.status for c in cases}
    assert by_id["tests/test_x.py::test_a"] == "failed"
    assert by_id["tests/test_x.py::test_b"] == "passed"


def test_parse_junit_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(OracleError):
        parse_junit(tmp_path / "nope.xml")


def test_run_command_timeout(tmp_path: Path) -> None:
    log = tmp_path / "log.txt"
    result = run_command(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        cwd=tmp_path,
        env=build_eval_env(),
        timeout_s=0.5,
        log_path=log,
    )

    assert result.timed_out
    assert log.exists()


def test_empty_group_passes(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)
    evidence = tmp_path / "evidence"

    group = evaluate_group(
        [],
        None,
        verification_dir=ws,
        evidence_dir=evidence,
        name="hidden",
        env=build_eval_env(),
    )

    assert group.status == "passed"
    assert group.total == 0


def test_relative_evidence_dir_still_finds_junit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = _make_workspace(tmp_path, fixed=True)
    monkeypatch.chdir(tmp_path)

    group = evaluate_group(
        [PY + ["tests/test_pricing.py"]],
        None,
        verification_dir=ws,
        evidence_dir=Path("relative-evidence"),
        name="f2p",
        env=build_eval_env(),
    )

    assert group.status == "passed", group.error
    assert (tmp_path / "relative-evidence" / "f2p_0.xml").is_file()
