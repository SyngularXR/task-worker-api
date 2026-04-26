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
