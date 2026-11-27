import pytest

from div import divide


def test_divide_by_zero_raises():
    with pytest.raises(ValueError):
        divide(1, 0)
