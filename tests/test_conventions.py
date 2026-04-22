"""Filename-convention helpers shared across worker + backend."""
from task_worker_api.conventions import preview_filename, finalized_filename


def test_preview_filename():
    assert preview_filename("skull_001") == "skull_001_simplified_preview.glb"


def test_finalized_filename():
    assert finalized_filename("skull_001") == "skull_001_finalized.glb"


def test_filenames_for_empty_base():
    assert preview_filename("") == "_simplified_preview.glb"
    assert finalized_filename("") == "_finalized.glb"
