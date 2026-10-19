import pytest

from task_worker_api.windows_supervisor import check_environment


@pytest.mark.parametrize('environment', [
    {'TEMP': 'x'},
    {'SystemRoot': 'a', 'SYSTEMROOT': 'b'},
    {'1X': 'y'},
    {'OK': 1},
])
def test_invalid_windows_environment(environment):
    with pytest.raises(ValueError):
        check_environment(environment)


def test_valid_windows_environment():
    assert check_environment({'HF_HOME': 'C:/m'}) is None
