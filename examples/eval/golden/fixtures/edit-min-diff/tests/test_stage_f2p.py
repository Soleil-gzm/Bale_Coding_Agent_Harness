from stages import run_stage


def test_stage_reports_three_retries():
    assert run_stage("build") == "stage:build:3"
