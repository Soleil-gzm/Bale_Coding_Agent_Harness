from stages import run_stage


def test_stage_name_is_preserved():
    assert run_stage("build").startswith("stage:build:")
