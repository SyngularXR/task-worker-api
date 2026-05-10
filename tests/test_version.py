"""Guard against drift between the package __version__ constant and wheel metadata."""
from importlib.metadata import version

import task_worker_api


def test_version_matches_wheel_metadata():
    assert task_worker_api.__version__ == version("task-worker-api")
