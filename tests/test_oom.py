import pytest

from task_worker_api.resources import is_out_of_memory


class OutOfMemoryError(RuntimeError):
    pass


@pytest.mark.parametrize("error,expected", [
    (MemoryError(), True), (OutOfMemoryError("allocation failed"), True),
    (RuntimeError("child: CUDA out of memory. Tried to allocate 1 GiB"), True),
    (RuntimeError("No space left on device"), False), (RuntimeError("exit 137"), False),
])
def test_allocation_failure_classification(error, expected):
    assert is_out_of_memory(error) is expected
