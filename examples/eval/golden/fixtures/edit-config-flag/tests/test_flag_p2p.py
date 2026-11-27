import settings


def test_timeout_unchanged():
    assert settings.TIMEOUT_SECONDS == 30
