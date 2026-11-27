"""Markdown rendering coverage for the hermetic and preflight sections (E4)."""

from bale_harness.eval import (
    DatasetFingerprint,
    EvalManifest,
    EvalReport,
    GitFingerprint,
    ModelFingerprint,
    PreflightResult,
    PromptFingerprint,
    SandboxFingerprint,
    TaskOutcome,
    ToolFingerprint,
    render_markdown,
)


def _report(*, hermetic: bool) -> EvalReport:
    manifest = EvalManifest(
        run_id="run",
        created_at="2026-01-01T00:00:00+00:00",
        task_count=1,
        git=GitFingerprint(commit="abc123", dirty=False),
        model=ModelFingerprint(provider="fake", model="fake", base_url=""),
        prompt=PromptFingerprint(system_prompt_sha256="p", messages_sha256="m"),
        tools=ToolFingerprint(schema_sha256="t", names=["read_file"]),
        sandbox=SandboxFingerprint(
            backend="docker",
            image="img",
            network=False,
            memory="1g",
            cpus=1.0,
            pids_limit=256,
            user="1000:1000",
            image_digest="sha256:deadbeef",
        ),
        dataset=DatasetFingerprint(
            suite="golden", suite_version="1", content_sha256="d", file_count=1
        ),
        hermetic=hermetic,
    )
    outcome = TaskOutcome(
        task_id="t1",
        category="edit",
        repeat_index=0,
        status="passed",  # type: ignore[arg-type]
        duration_ms=1,
    )
    return EvalReport(
        started_at="t",
        duration_ms=1,
        total=1,
        passed=1,
        failed=0,
        errors=0,
        outcomes=[outcome],
        manifest=manifest,
    )


def test_render_markdown_shows_hermetic_yes() -> None:
    assert "Hermetic: yes" in render_markdown(_report(hermetic=True))


def test_render_markdown_shows_hermetic_no_for_local() -> None:
    assert "Hermetic: no" in render_markdown(_report(hermetic=False))


def test_render_markdown_includes_preflight_section() -> None:
    report = _report(hermetic=True)
    report.preflight = [
        PreflightResult(
            task_id="edit-add-negative",
            ok=True,
            pristine_p2p_passed=True,
            pristine_p2p_passed_cases=["p2p-case-1"],
            gold_all_passed=True,
        )
    ]

    markdown = render_markdown(report)

    assert "## Preflight" in markdown
    assert "edit-add-negative" in markdown
    assert "1 passed" in markdown


def test_render_markdown_omits_preflight_when_absent() -> None:
    report = _report(hermetic=True)
    report.preflight = None

    assert "## Preflight" not in render_markdown(report)
