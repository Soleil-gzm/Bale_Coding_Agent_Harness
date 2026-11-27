import settings


def test_debug_is_disabled():
    assert settings.DEBUG is False
