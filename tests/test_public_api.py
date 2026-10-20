"""Public helper aliases preserve the existing private import paths."""

import task_worker_api
from task_worker_api import require_safe_filename, to_thread_complete
from task_worker_api.client import _to_thread_complete
from task_worker_api.files import _require_safe_filename


def test_public_names_import():
    assert callable(require_safe_filename)
    assert callable(to_thread_complete)


def test_public_names_in_all():
    assert {"require_safe_filename", "to_thread_complete"} <= set(task_worker_api.__all__)


def test_private_names_still_import():
    assert callable(_require_safe_filename)
    assert callable(_to_thread_complete)


def test_require_safe_filename_identity():
    assert require_safe_filename is _require_safe_filename


def test_to_thread_complete_identity():
    assert to_thread_complete is _to_thread_complete
