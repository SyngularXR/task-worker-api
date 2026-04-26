"""Integration tests that exercise paths FakeBackendClient cannot.

The protocol-drift capture lives inside the real BackendClient.claim_next,
so we mock the HTTP transport and use the real client. FakeBackendClient
bypasses claim_next entirely and would give us false confidence.

Worker.run_forever() startup/finally code paths also need real exercise —
Worker.run_one() bypasses startup INFO logging, cleanup, and the finally-
block close().
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from task_worker_api.client import BackendClient
from task_worker_api.enums import TaskType
from task_worker_api.payload_log import PayloadLogger


@pytest.mark.asyncio
async def test_backend_client_accepts_payload_logger(tmp_path: Path):
    logger = PayloadLogger(
        root=tmp_path / "_wp" / "w", worker_id="w", enabled=True,
        _boot_id="aaaaaaaa", _pid=lambda: 1,
    )
    client = BackendClient(
        "http://fake/api/v1", "key", payload_logger=logger,
    )
    assert client._payload_logger is logger
    await client.close()
