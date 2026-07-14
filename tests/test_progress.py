"""Unit tests for ProgressReporter.

Covers the paths the worker-loop integration tests don't reach directly:
  - update() emits an immediate progress call and propagates cancel signals
  - update() swallows backend errors (best-effort contract)
  - start_heartbeat() rejects a double start
  - stop() is a no-op when never started and swallows loop errors
  - is_cancelled / raise_if_cancelled reflect the shared cancelled event
  - the background heartbeat loop emits periodic reports and tolerates errors
  - link_cancelled() merges an external CancelGuard event into is_cancelled
"""
from __future__ import annotations

import asyncio

import pytest

from task_worker_api.errors import TaskCancelled
from task_worker_api.progress import ProgressReporter, _SharedState
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


@pytest.mark.asyncio
async def test_heartbeat_escalates_to_warning_after_threshold(caplog):
    """Sustained backend failures (>= heartbeat_warn_threshold, default 3)
    must escalate from DEBUG to WARNING so a long-running worker (colmap-splat,
    Neural-Canvas) surfaces an unreachable backend at the default log level
    instead of silently masking it at DEBUG until the sweeper reclaims the
    task. Each tick past the threshold re-warns so the outage stays loud."""

    class _DeadClient(FakeBackendClient):
        async def report_progress(self, task_id, *, stage, current=0, total=0,
                                  kill_handle=None):
            raise ConnectionError("backend unreachable")

    client = _DeadClient()
    pr = ProgressReporter(client, task_id=1, heartbeat_interval_s=0.01)
    with caplog.at_level("DEBUG"):
        await pr.start_heartbeat()
        await asyncio.sleep(0.10)  # >= 3 ticks at 0.01s
        await pr.stop()

    warnings = [r for r in caplog.records if r.levelname == "WARNING"
                and "consecutive failures" in r.message]
    debugs = [r for r in caplog.records if r.levelname == "DEBUG"
              and "heartbeat failed" in r.message]
    # At least one WARNING fired once the threshold was crossed.
    assert warnings, "expected at least one WARNING after threshold"
    # The first (threshold - 1) failures stayed at DEBUG.
    assert debugs, "expected sub-threshold failures at DEBUG"


@pytest.mark.asyncio
async def test_heartbeat_resets_failure_count_on_success(caplog):
    """A successful tick after failures must reset the consecutive-failure
    counter, so a future failure sequence starts again from DEBUG rather than
    accumulating toward a spurious WARNING from a prior blip."""

    class _RecoveringClient(FakeBackendClient):
        def __init__(self):
            super().__init__()
            self._calls = 0

        async def report_progress(self, task_id, *, stage, current=0, total=0,
                                  kill_handle=None):
            self._calls += 1
            # 2 failures (below default threshold of 3), then recover.
            if self._calls <= 2:
                raise ConnectionError("transient")
            return await super().report_progress(
                task_id, stage=stage, current=current, total=total,
                kill_handle=kill_handle,
            )

    client = _RecoveringClient()
    pr = ProgressReporter(client, task_id=1, heartbeat_interval_s=0.01)
    with caplog.at_level("DEBUG"):
        await pr.start_heartbeat()
        await asyncio.sleep(0.08)  # 2 failures + several successes
        await pr.stop()

    # 2 failures < threshold(3) → no WARNING should have fired.
    warnings = [r for r in caplog.records if r.levelname == "WARNING"
                and "consecutive failures" in r.message]
    assert not warnings, "sub-threshold failures must not warn"
    # Counter reset: failures stayed DEBUG and successes followed.
    assert client._calls >= 4
    assert len(client.progress_events) >= 2


@pytest.mark.asyncio
async def test_heartbeat_warn_threshold_is_configurable(caplog):
    """heartbeat_warn_threshold must be tunable so a worker that wants every
    failure surfaced (threshold=1) can get it, or one that tolerates more
    churn can raise it. With threshold=1 the first failure already warns."""

    class _DeadClient(FakeBackendClient):
        async def report_progress(self, task_id, *, stage, current=0, total=0,
                                  kill_handle=None):
            raise ConnectionError("backend unreachable")

    client = _DeadClient()
    pr = ProgressReporter(
        client, task_id=1,
        heartbeat_interval_s=0.01,
        heartbeat_warn_threshold=1,
    )
    with caplog.at_level("DEBUG"):
        await pr.start_heartbeat()
        await asyncio.sleep(0.05)
        await pr.stop()

    # threshold=1 → the very first failure is a WARNING, none at DEBUG.
    warnings = [r for r in caplog.records if r.levelname == "WARNING"
                and "consecutive failures" in r.message]
    debugs = [r for r in caplog.records if r.levelname == "DEBUG"
              and "heartbeat failed" in r.message]
    assert warnings, "threshold=1 must warn on the first failure"
    assert not debugs, "threshold=1 must leave no failure at DEBUG"


@pytest.mark.asyncio
async def test_heartbeat_warn_threshold_clamped_to_one():
    """A threshold < 1 would never fire (failures can't reach 0 negatives),
    silently disabling the escalation. Clamp to 1 so a misconfigured 0 still
    surfaces outages rather than swallowing them."""

    pr = ProgressReporter(
        FakeBackendClient(), task_id=1,
        heartbeat_warn_threshold=0,
    )
    assert pr._warn_threshold == 1


# ----- link_cancelled (external CancelGuard event) ---------------------------


def test_is_cancelled_false_without_linked_event():
    """Default: no external event linked, is_cancelled is False."""
    pr = ProgressReporter(FakeBackendClient(), task_id=1)
    assert pr.is_cancelled is False


def test_link_cancelled_reflects_external_event():
    """When an external event is linked and set, is_cancelled is True even
    if the heartbeat has never reported cancel."""
    pr = ProgressReporter(FakeBackendClient(), task_id=1)
    ext = asyncio.Event()
    pr.link_cancelled(ext)

    assert pr.is_cancelled is False
    ext.set()
    assert pr.is_cancelled is True


def test_link_cancelled_raise_if_cancelled_raises():
    """raise_if_cancelled must fire when the linked external event is set,
    without needing a heartbeat tick."""
    pr = ProgressReporter(FakeBackendClient(), task_id=1)
    ext = asyncio.Event()
    pr.link_cancelled(ext)
    ext.set()

    with pytest.raises(TaskCancelled):
        pr.raise_if_cancelled()


def test_link_cancelled_none_unlinks():
    """Passing None to link_cancelled unlinks a previously-linked event so
    it no longer affects is_cancelled."""
    pr = ProgressReporter(FakeBackendClient(), task_id=1)
    ext = asyncio.Event()
    pr.link_cancelled(ext)
    ext.set()
    assert pr.is_cancelled is True

    pr.link_cancelled(None)
    assert pr.is_cancelled is False


def test_is_cancelled_true_when_either_event_set():
    """is_cancelled is True if either the heartbeat's own event OR the
    linked external event is set."""
    pr = ProgressReporter(FakeBackendClient(), task_id=1)
    ext = asyncio.Event()
    pr.link_cancelled(ext)

    # Heartbeat event set, external not → True
    pr._state.cancelled.set()
    assert pr.is_cancelled is True

    # Reset heartbeat, set external → True
    pr._state = _SharedState()  # fresh state, not cancelled
    pr.link_cancelled(ext)
    ext.set()
    assert pr.is_cancelled is True

    # Both unset → False
    pr.link_cancelled(None)
    pr._state = _SharedState()
    assert pr.is_cancelled is False


def test_link_cancelled_safe_before_or_after_start():
    """link_cancelled can be called before start_heartbeat or after stop
    without error — the link is independent of the heartbeat task
    lifecycle."""
    pr = ProgressReporter(FakeBackendClient(), task_id=1,
                          heartbeat_interval_s=10.0)
    # Before start — fine.
    ext = asyncio.Event()
    pr.link_cancelled(ext)
    pr.link_cancelled(None)
