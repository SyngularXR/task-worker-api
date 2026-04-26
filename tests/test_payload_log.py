"""Unit tests for PayloadLogger.

PayloadLogger captures task envelopes to JSONL files inside a worker's
shared volume mount. These tests use ``tmp_path`` so they don't need a
real /app/shared mount; the production root is wired by Worker.__init__.
"""
from __future__ import annotations

import re
from pathlib import Path

from task_worker_api.payload_log import PayloadLogger


def test_disabled_logger_does_not_create_root(tmp_path: Path):
    """Constructing with enabled=False must not touch the filesystem."""
    root = tmp_path / "_worker_payloads" / "test-worker"
    PayloadLogger(root=root, worker_id="test-worker", enabled=False)
    assert not root.exists()


def test_enabled_init_creates_root_directory(tmp_path: Path):
    root = tmp_path / "_worker_payloads" / "test-worker"
    PayloadLogger(root=root, worker_id="test-worker", enabled=True)
    assert root.is_dir()


def test_boot_id_is_8_hex_chars_by_default(tmp_path: Path):
    logger = PayloadLogger(
        root=tmp_path / "_worker_payloads" / "test-worker",
        worker_id="test-worker",
        enabled=True,
    )
    assert re.fullmatch(r"[0-9a-f]{8}", logger.boot_id) is not None


def test_boot_id_can_be_injected_for_tests(tmp_path: Path):
    logger = PayloadLogger(
        root=tmp_path / "_worker_payloads" / "test-worker",
        worker_id="test-worker",
        enabled=True,
        _boot_id="deadbeef",
    )
    assert logger.boot_id == "deadbeef"


# ----- sanitize_worker_id ----------------------------------------------------

from task_worker_api.payload_log import sanitize_worker_id


def test_sanitize_replaces_unsafe_chars():
    assert sanitize_worker_id("blender-worker-1") == "blender-worker-1"
    assert sanitize_worker_id("worker/1") == "worker_1"
    assert sanitize_worker_id("worker\\1") == "worker_1"
    assert sanitize_worker_id("worker:1") == "worker_1"
    assert sanitize_worker_id("../etc") == ".._etc"  # `.` is allowed; `/` becomes `_`


def test_sanitize_rejects_dot_only_names():
    assert sanitize_worker_id(".") == "._x"
    assert sanitize_worker_id("..") == ".._x"
    assert sanitize_worker_id("") == "_x"


def test_sanitize_rejects_windows_reserved_names():
    assert sanitize_worker_id("CON") == "CON_x"
    assert sanitize_worker_id("nul") == "nul_x"
    assert sanitize_worker_id("COM3") == "COM3_x"
    assert sanitize_worker_id("LPT9") == "LPT9_x"
    # extension after reserved name is still reserved on Windows
    assert sanitize_worker_id("CON.log") == "CON.log_x"


def test_sanitize_output_never_contains_separators():
    assert "/" not in sanitize_worker_id("worker/1")
    assert "\\" not in sanitize_worker_id("worker\\1")
