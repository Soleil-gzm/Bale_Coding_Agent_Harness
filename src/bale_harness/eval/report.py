"""Markdown rendering for evaluation reports."""

from bale_harness.eval.metrics import EvalMetrics
from bale_harness.eval.runner import EvalReport


def _cell(text: str, limit: int = 300) -> str:
    compact = " ".join(text.split())
    if len(compact) > limit:
        compact = compact[:limit].rstrip() + "..."
    return compact.replace("|", "\\|")


def _marker(passed: bool) -> str:
    return "PASS" if passed else "FAIL"


def _metric_value(value: float | int | None) -> str:
    if value is None:
        return "null"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def _metrics_section(metrics: EvalMetrics) -> list[str]:
    coverage = metrics.coverage
    rows = [
        ("scheduled attempts", _metric_value(metrics.scheduled), "all task runs"),
        ("resolved", _metric_value(metrics.resolved), "status == passed"),
        ("resolve rate", _metric_value(metrics.resolve_rate), "resolved / scheduled"),
        ("pass@1", _metric_value(metrics.pass_at_1), "first attempt per task"),
        (
            "pass@k",
            _metric_value(metrics.pass_at_k),
            coverage.get("pass_at_k", "any attempt per task"),
        ),
        (
            "test cheating rate",
            _metric_value(metrics.test_cheating_rate),
            "cheating/forbidden codes",
        ),
        (
            "security violation rate",
            _metric_value(metrics.security_violation_rate),
            coverage.get("security_violation_rate", "security attempts"),
        ),
        (
            "infrastructure error rate",
            _metric_value(metrics.infrastructure_error_rate),
            "status == error",
        ),
        (
            "duration p95 (ms)",
            _metric_value(metrics.duration_p95_ms),
            f"n={metrics.sample_size}",
        ),
    ]
    for name, value, note in (
        ("cost per success", metrics.cost_per_success, coverage["cost_per_success"]),
        ("regression rate", metrics.regression_rate, coverage["regression_rate"]),
        (
            "repeated call rate",
            metrics.repeated_call_rate,
            coverage["repeated_call_rate"],
        ),
    ):
        rows.append((name, _metric_value(value), note))

    lines = [
        "",
        "## Metrics",
        "",
        "| Metric | Value | Coverage |",
        "| --- | --- | --- |",
    ]
    for name, value, note in rows:
        lines.append(f"| {name} | {value} | {_cell(note)} |")
    return lines


def render_markdown(report: EvalReport) -> str:
    """Render an :class:`EvalReport` as a self-contained Markdown document."""
    result = "PASS" if report.ok else "FAIL"
    lines = [
        "# Evaluation Report",
        "",
        f"- Started: {report.started_at}",
        f"- Duration: {report.duration_ms} ms",
        f"- Tasks: {report.total} (passed {report.passed}, "
        f"failed {report.failed}, errors {report.errors})",
        f"- Result: {result}",
        "",
    ]
    if report.metrics is not None:
        lines.extend(_metrics_section(report.metrics))
        lines.append("")
    lines.extend(
        [
            "| Task | Status | Run | Iterations | Tools | Tokens | Checks |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for outcome in report.outcomes:
        checks = outcome.check_results
        check_summary = f"{sum(1 for item in checks if item.passed)}/{len(checks)}"
        lines.append(
            f"| `{outcome.task_id}` | {outcome.status} "
            f"| {outcome.run_status or '-'} | {outcome.iterations} "
            f"| {outcome.tool_calls} | {outcome.total_tokens} "
            f"| {check_summary} |"
        )

    lines.extend(["", "## Details"])
    for outcome in report.outcomes:
        lines.extend(["", f"### `{outcome.task_id}`", ""])
        if outcome.description:
            lines.append(f"- Description: {_cell(outcome.description)}")
        lines.append(f"- Status: {outcome.status}")
        lines.append(f"- Run status: {outcome.run_status or '-'}")
        if outcome.answer:
            lines.append(f"- Answer: {_cell(outcome.answer)}")
        if outcome.error:
            lines.append(f"- Error: {_cell(outcome.error)}")
        if outcome.check_results:
            lines.append("- Checks:")
            for item in outcome.check_results:
                lines.append(
                    f"  - {_marker(item.passed)} `{item.check.type}` "
                    f"`{item.check.path}` — {_cell(item.detail)}"
                )
        else:
            lines.append("- Checks: none")
        if outcome.integrity is not None:
            integrity = outcome.integrity
            verdict = "ok" if integrity.ok else "VIOLATION"
            lines.append(
                f"- Integrity: {verdict} "
                f"({integrity.files_changed} files, "
                f"+{integrity.lines_added}/-{integrity.lines_removed})"
            )
            for violation in integrity.violations:
                where = f"`{violation.path}` " if violation.path else ""
                lines.append(f"  - {violation.code} {where}— {_cell(violation.detail)}")
        if outcome.oracle is not None:
            oracle = outcome.oracle
            verdict = "passed" if oracle.all_passed else "FAILED"
            lines.append(
                f"- Oracle: {verdict} "
                f"(f2p {oracle.f2p.status}, p2p {oracle.p2p.status}, "
                f"hidden {oracle.hidden.status})"
            )
    return "\n".join(lines) + "\n"
