"""Unit tests for ProgressReporter.

Covers the paths the worker-loop integration tests don't reach directly:
  - update() emits an immediate progress call and propagates cancel signals
  - update() swallows backend errors (best-effort contract)
  - start_heartbeat() rejects a double start
  - stop() is a no-op when never started and swallows loop errors
  - is_cancelled / raise_if_cancelled reflect the shared cancelled event
  - the background heartbeat loop emits periodic reports and tolerates errors
"""
from __future__ import annotations

import asyncio

import pytest

from task_worker_api.errors import TaskCancelled
from task_worker_api.progress import ProgressReporter
from task_worker_api.testing import FakeBackendClient


# ----- update() --------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_emits_immediate_progress_with_stage():
    client = FakeBackendClient()
    pr = ProgressReporter(client, task_id=1)
    await pr.update("processing", current=3, total=10)

    assert len(client.progress_events) == 1
    ev = client.progress_events[0]
    assert ev["stage"] == "processing"
    assert ev["current"] == 3
    assert ev["total"] == 10


@pytest.mark.asyncio
async def test_update_sets_cancelled_when_backend_reports_cancel():
    client = FakeBackendClient()
    client.mark_cancelled(1)
    pr = ProgressReporter(client, task_id=1)

    assert pr.is_cancelled is False
    await pr.update("processing")
    assert pr.is_cancelled is True


@pytest.mark.asyncio
async def test_update_swallows_backend_error(caplog):
    """A flaky report_progress must not crash the handler that called update."""

    class _BoomClient(FakeBackendClient):
        async def report_progress(self, task_id, *, stage, current=0, total=0,
                                  kill_handle=None):
            raise ConnectionError("backend down")

    pr = ProgressReporter(_BoomClient(), task_id=1)
    with caplog.at_level("WARNING"):
        await pr.update("processing", current=1, total=2)

    assert any("progress update failed" in r.message for r in caplog.records)
    assert pr.is_cancelled is False


# ----- start_heartbeat / stop ------------------------------------------------


@pytest.mark.asyncio
async def test_start_heartbeat_rejects_double_start():
    pr = ProgressReporter(FakeBackendClient(), task_id=1,
                          heartbeat_interval_s=10.0)
    await pr.start_heartbeat()
    try:
        with pytest.raises(RuntimeError, match="heartbeat already running"):
            await pr.start_heartbeat()
    finally:
        await pr.stop()


@pytest.mark.asyncio
async def test_stop_is_noop_when_never_started():
    pr = ProgressReporter(FakeBackendClient(), task_id=1)
    await pr.stop()  # must not raise


@pytest.mark.asyncio
async def test_stop_swallows_loop_exception():
    """If the heartbeat task raised, stop() must still clean up without
    surfacing the error to the worker's finally block."""

    class _BoomClient(FakeBackendClient):
        async def report_progress(self, task_id, *, stage, current=0, total=0,
                                  kill_handle=None):
            raise RuntimeError("boom")

    pr = ProgressReporter(_BoomClient(), task_id=1, heartbeat_interval_s=0.01)
    await pr.start_heartbeat()
    await asyncio.sleep(0.05)  # let a couple of ticks fire
    await pr.stop()  # must not raise
    assert pr._task is None


# ----- is_cancelled / raise_if_cancelled ------------------------------------


@pytest.mark.asyncio
async def test_raise_if_cancelled_raises_task_cancelled():
    client = FakeBackendClient()
    client.mark_cancelled(7)
    pr = ProgressReporter(client, task_id=7)
    await pr.update("processing")  # propagates the cancel signal

    with pytest.raises(TaskCancelled):
        pr.raise_if_cancelled()


def test_raise_if_cancelled_noop_when_not_cancelled():
    pr = ProgressReporter(FakeBackendClient(), task_id=1)
    pr.raise_if_cancelled()  # must not raise


# ----- background heartbeat loop ---------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_loop_emits_periodic_reports():
    client = FakeBackendClient()
    pr = ProgressReporter(client, task_id=1, heartbeat_interval_s=0.01)
    await pr.start_heartbeat()
    await asyncio.sleep(0.05)
    await pr.stop()

    assert len(client.progress_events) >= 2
    assert all(ev["task_id"] == 1 for ev in client.progress_events)


@pytest.mark.asyncio
async def test_heartbeat_loop_propagates_cancel_signal():
    client = FakeBackendClient()
    client.mark_cancelled(1)
    pr = ProgressReporter(client, task_id=1, heartbeat_interval_s=0.01)
    await pr.start_heartbeat()
    await asyncio.sleep(0.05)
    await pr.stop()

    assert pr.is_cancelled is True


@pytest.mark.asyncio
async def test_heartbeat_loop_tolerates_transient_errors(caplog):
    """A transient backend failure in the heartbeat must not kill the loop;
    the next tick should succeed."""

    class _FlakyClient(FakeBackendClient):
        def __init__(self):
            super().__init__()
            self._calls = 0

        async def report_progress(self, task_id, *, stage, current=0, total=0,
                                  kill_handle=None):
            self._calls += 1
            if self._calls == 1:
                raise ConnectionError("transient")
            return await super().report_progress(
                task_id, stage=stage, current=current, total=total,
                kill_handle=kill_handle,
            )

    client = _FlakyClient()
    pr = ProgressReporter(client, task_id=1, heartbeat_interval_s=0.01)
    with caplog.at_level("DEBUG"):
        await pr.start_heartbeat()
        await asyncio.sleep(0.05)
        await pr.stop()

    # The loop survived the first error and kept ticking.
    assert client._calls >= 2
    assert len(client.progress_events) >= 1
