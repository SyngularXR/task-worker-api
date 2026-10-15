"""Guard fleet routing, the release-notes tag, and SDK pin placeholder rendering."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from task_worker_api import TASK_PARAMS_SCHEMAS, TaskType

_MANIFEST = Path(__file__).resolve().parent.parent / "docs" / "fleet" / "workers.json"
_manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
_WORKERS = _manifest["workers"]


def test_worker_ids_are_unique():
    ids = [worker["id"] for worker in _WORKERS]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("worker", _WORKERS, ids=[w["id"] for w in _WORKERS])
def test_task_types_are_registered(worker):
    for value in worker["task_types"]:
        assert TaskType(value) in TASK_PARAMS_SCHEMAS, (
            f"{worker['id']}: no params schema for task type {value!r}"
        )


def test_release_notes_match_current_release():
    sdk = _manifest["sdk"]
    assert sdk["release_notes"].endswith("/releases/tag/" + sdk["current_release"])


@pytest.mark.parametrize("worker", _WORKERS, ids=[w["id"] for w in _WORKERS])
def test_line_pattern_has_version_placeholder(worker):
    assert "{version}" in worker["sdk_pin"]["line_pattern"]


@pytest.mark.parametrize("worker", _WORKERS, ids=[w["id"] for w in _WORKERS])
def test_line_pattern_renders_exact_tag(worker):
    pin = worker["sdk_pin"]
    version = pin["version"]
    assert version.startswith("v")
    rendered = pin["line_pattern"].replace("{version}", version.removeprefix("v"))
    assert version in rendered
    assert "vv" not in rendered
