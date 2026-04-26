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


def _make_logger(tmp_path: Path) -> PayloadLogger:
    return PayloadLogger(
        root=tmp_path / "_wp" / "w", worker_id="w", enabled=True,
        _boot_id="aaaaaaaa", _pid=lambda: 1,
    )


def _make_client(handler, logger: PayloadLogger) -> BackendClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(
        base_url="http://fake/api/v1", transport=transport,
        headers={"Authorization": "Bearer x"},
    )
    return BackendClient(
        "http://fake/api/v1", "x", client=http, payload_logger=logger,
    )


@pytest.mark.asyncio
async def test_backend_client_accepts_payload_logger(tmp_path: Path):
    logger = _make_logger(tmp_path)
    client = BackendClient(
        "http://fake/api/v1", "key", payload_logger=logger,
    )
    assert client._payload_logger is logger
    await client.close()


@pytest.mark.asyncio
async def test_claim_next_captures_raw_on_json_parse_failure(tmp_path: Path):
    """Backend returns invalid JSON (e.g. an HTML 500 page with status 200).
    The raw response text and the JSONDecodeError must be captured."""
    logger = _make_logger(tmp_path)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"<html><body>500 Internal Server Error</body></html>",
            headers={"content-type": "text/html"},
        )

    client = _make_client(handler, logger)
    with pytest.raises(Exception):
        await client.claim_next([TaskType.DETECT_CUT_PLANES], worker_id="w")
    await client.close()
    logger.close()

    raw_files = list((tmp_path / "_wp" / "w").glob("raw_envelopes-*.jsonl"))
    assert len(raw_files) == 1
    entry = json.loads(raw_files[0].read_text(encoding="utf-8"))
    assert entry["error_type"] == "JSONDecodeError"
    assert "html" in entry["raw"].lower()


@pytest.mark.asyncio
async def test_claim_next_captures_raw_on_from_dict_failure(tmp_path: Path):
    """Backend returns valid JSON but with an unknown task_type —
    ClaimedTask.from_dict raises; raw envelope must be captured."""
    logger = _make_logger(tmp_path)

    bad_body = {
        "id": 99,
        "task_type": "unknown_future_type",
        "status": 2,
        "case_id": 1,
        "item_key": "x",
        "params": {"k": "v"},
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=bad_body)

    client = _make_client(handler, logger)
    with pytest.raises(Exception):
        await client.claim_next([TaskType.DETECT_CUT_PLANES], worker_id="w")
    await client.close()
    logger.close()

    raw_files = list((tmp_path / "_wp" / "w").glob("raw_envelopes-*.jsonl"))
    assert len(raw_files) == 1
    entry = json.loads(raw_files[0].read_text(encoding="utf-8"))
    assert entry["raw"] == bad_body
    assert entry["error_type"] == "ValueError"
    assert "unknown_future_type" in entry["error"]


@pytest.mark.asyncio
async def test_claim_next_no_raw_on_204(tmp_path: Path):
    """A healthy 204 (no task available) must not write any raw envelope."""
    logger = _make_logger(tmp_path)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    client = _make_client(handler, logger)
    result = await client.claim_next([TaskType.DETECT_CUT_PLANES], worker_id="w")
    assert result is None
    await client.close()
    logger.close()

    assert list((tmp_path / "_wp" / "w").glob("raw_envelopes-*.jsonl")) == []


# ----- Worker.run_forever lifecycle -----------------------------------------

import os
import time

from task_worker_api.testing import FakeBackendClient
from task_worker_api.worker import Worker


async def _shutdown_after(worker: Worker, delay_s: float):
    await asyncio.sleep(delay_s)
    await worker.shutdown()


@pytest.mark.asyncio
async def test_run_forever_logs_startup_state(tmp_path: Path, caplog):
    fake = FakeBackendClient()
    shared = tmp_path / "shared"
    worker = Worker(
        backend_url="http://fake/api/v1", api_key="k", worker_id="w",
        handlers={}, work_dir=str(tmp_path / "work"), client=fake,
        shared_volume_path=str(shared),
        poll_interval_s=0.05,
    )

    asyncio.create_task(_shutdown_after(worker, 0.1))
    with caplog.at_level("INFO"):
        await asyncio.wait_for(worker.run_forever(), timeout=2.0)

    text = "\n".join(r.message for r in caplog.records)
    assert "payload logging" in text.lower()
    assert "enabled" in text.lower()


@pytest.mark.asyncio
async def test_run_forever_runs_startup_cleanup(tmp_path: Path):
    """Pre-existing expired files in the worker dir are gone after startup."""
    shared = tmp_path / "shared"
    worker_dir = shared / "_worker_payloads" / "w"
    worker_dir.mkdir(parents=True)
    expired = worker_dir / "payloads-2026-03-27-pid1-aaaa.jsonl"
    expired.write_text("{}\n", encoding="utf-8")
    age_s = 30 * 86400
    os.utime(expired, (time.time() - age_s, time.time() - age_s))

    fake = FakeBackendClient()
    worker = Worker(
        backend_url="http://fake/api/v1", api_key="k", worker_id="w",
        handlers={}, work_dir=str(tmp_path / "work"), client=fake,
        shared_volume_path=str(shared),
        poll_interval_s=0.05,
    )
    asyncio.create_task(_shutdown_after(worker, 0.1))
    await asyncio.wait_for(worker.run_forever(), timeout=2.0)

    assert not expired.exists()


@pytest.mark.asyncio
async def test_run_forever_closes_logger_in_finally(tmp_path: Path):
    fake = FakeBackendClient()
    shared = tmp_path / "shared"
    worker = Worker(
        backend_url="http://fake/api/v1", api_key="k", worker_id="w",
        handlers={}, work_dir=str(tmp_path / "work"), client=fake,
        shared_volume_path=str(shared),
        poll_interval_s=0.05,
    )
    asyncio.create_task(_shutdown_after(worker, 0.05))
    await asyncio.wait_for(worker.run_forever(), timeout=2.0)
    assert worker._payload_logger._handles == {}


@pytest.mark.asyncio
async def test_run_forever_periodic_cleanup(tmp_path: Path, monkeypatch):
    """With a tight cleanup interval, the timer fires multiple times during the run."""
    monkeypatch.setenv("WORKER_PAYLOAD_LOG_CLEANUP_INTERVAL_S", "0.05")
    fake = FakeBackendClient()
    shared = tmp_path / "shared"
    worker = Worker(
        backend_url="http://fake/api/v1", api_key="k", worker_id="w",
        handlers={}, work_dir=str(tmp_path / "work"), client=fake,
        shared_volume_path=str(shared),
        poll_interval_s=0.01,
    )

    counter = {"n": 0}
    real_cleanup = worker._payload_logger.cleanup_old_files

    def counting_cleanup():
        counter["n"] += 1
        real_cleanup()

    worker._payload_logger.cleanup_old_files = counting_cleanup  # type: ignore[method-assign]

    asyncio.create_task(_shutdown_after(worker, 0.3))
    await asyncio.wait_for(worker.run_forever(), timeout=2.0)

    # 1 startup + at least 2 periodic firings during 300ms with 50ms interval.
    assert counter["n"] >= 3
