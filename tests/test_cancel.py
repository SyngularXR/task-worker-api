"""Unit tests for CancelGuard.

Covers the cancel-polling paths the worker-loop integration tests reach only
indirectly through the full Worker.run_one cycle:
  - cancel detected → on_cancel hook fires → TaskCancelled raised on exit
  - on_cancel hook exception is swallowed (the poll loop must survive)
  - transient poll errors are tolerated (next tick retries)
  - no cancel → guard exits cleanly without raising
  - on_cancel omitted (None) is fine
"""
from __future__ import annotations

import asyncio

import pytest

import task_worker_api.cancel as cancel_mod
from task_worker_api.cancel import CancelGuard
from task_worker_api.enums import TaskStatus
from task_worker_api.errors import TaskCancelled
from task_worker_api.testing import FakeBackendClient


@pytest.mark.asyncio
async def test_cancel_detected_calls_on_cancel_and_raises():
    client = FakeBackendClient()
    client.mark_cancelled(1)
    called = []

    with pytest.raises(TaskCancelled):
        async with CancelGuard(
            client, task_id=1, poll_interval_s=0.01,
            on_cancel=lambda: called.append(True),
        ) as cancelled:
            # Wait for the poller to observe the cancel signal.
            await asyncio.wait_for(cancelled.wait(), timeout=2)
            # Exiting the context manager triggers the TaskCancelled raise.

    assert called == [True]


@pytest.mark.asyncio
async def test_on_cancel_exception_is_swallowed():
    """If the on_cancel hook raises, the guard must still set the cancelled
    event and raise TaskCancelled — a buggy hook must not strand the task."""

    class _BoomClient(FakeBackendClient):
        async def get_cancel_status(self, task_id):
            return {"cancelled": True}

    with pytest.raises(TaskCancelled):
        async with CancelGuard(
            _BoomClient(), task_id=1, poll_interval_s=0.01,
            on_cancel=lambda: (_ for _ in ()).throw(RuntimeError("hook bug")),
        ) as cancelled:
            await asyncio.wait_for(cancelled.wait(), timeout=2)


@pytest.mark.asyncio
async def test_poll_error_tolerated_then_cancel_detected(caplog):
    """A transient get_cancel_status failure must not kill the poll loop;
    the next successful poll should still surface the cancel."""

    class _FlakyClient(FakeBackendClient):
        def __init__(self):
            super().__init__()
            self._polls = 0

        async def get_cancel_status(self, task_id):
            self._polls += 1
            if self._polls == 1:
                raise ConnectionError("transient")
            return {"cancelled": True}

    client = _FlakyClient()
    with pytest.raises(TaskCancelled):
        with caplog.at_level("DEBUG"):
            async with CancelGuard(
                client, task_id=1, poll_interval_s=0.01,
            ) as cancelled:
                await asyncio.wait_for(cancelled.wait(), timeout=2)

    assert client._polls >= 2  # the first error didn't stop the loop


@pytest.mark.asyncio
async def test_no_cancel_exits_cleanly():
    """When the task is never cancelled, the guard exits without raising."""
    client = FakeBackendClient()

    async with CancelGuard(
        client, task_id=1, poll_interval_s=0.01,
    ) as cancelled:
        # Give the poller a couple of ticks, then exit normally.
        await asyncio.sleep(0.05)
        assert cancelled.is_set() is False


@pytest.mark.asyncio
async def test_on_cancel_none_is_fine():
    """The on_cancel hook is optional; omitting it must not error."""
    client = FakeBackendClient()
    client.mark_cancelled(1)

    with pytest.raises(TaskCancelled):
        async with CancelGuard(
            client, task_id=1, poll_interval_s=0.01,
        ) as cancelled:
            await asyncio.wait_for(cancelled.wait(), timeout=2)


@pytest.mark.asyncio
async def test_guard_cancels_poll_task_on_clean_exit():
    """On a clean (non-cancelled) exit the background poll task is cancelled
    and awaited — no lingering task warnings."""
    client = FakeBackendClient()

    async with CancelGuard(
        client, task_id=1, poll_interval_s=0.01,
    ) as cancelled:
        await asyncio.sleep(0.03)

    # If the poll task weren't properly cancelled we'd see a "Task was
    # destroyed but it is pending!" warning. pytest-asyncio surfaces those
    # as errors on some configs; a clean pass means the finally block worked.


@pytest.mark.asyncio
async def test_guard_uses_poll_cancel_status_not_get_cancel_status():
    """CancelGuard must call ``poll_cancel_status`` (one-shot, no retries)
    rather than ``get_cancel_status`` (which goes through ``_retry`` with
    exponential backoff). The guard has its own poll-interval retry, so
    the client-level backoff chain only delays cancel detection — a
    degraded backend could blind the guard for ~50s with the old path.
    """
    called_methods: list[str] = []

    class _TrackingClient(FakeBackendClient):
        async def get_cancel_status(self, task_id):
            called_methods.append("get_cancel_status")
            return await super().get_cancel_status(task_id)

        async def poll_cancel_status(self, task_id):
            called_methods.append("poll_cancel_status")
            # Return directly — don't delegate to get_cancel_status (which
            # would record itself in called_methods and muddle the assertion).
            return {
                "cancelled": task_id in self.cancelled_task_ids,
                "status": int(
                    TaskStatus.CANCELLED if task_id in self.cancelled_task_ids
                    else TaskStatus.IN_PROGRESS
                ),
                "cancelled_reason": (
                    "user" if task_id in self.cancelled_task_ids else None
                ),
            }

    client = _TrackingClient()
    client.mark_cancelled(1)

    with pytest.raises(TaskCancelled):
        async with CancelGuard(
            client, task_id=1, poll_interval_s=0.01,
        ) as cancelled:
            await asyncio.wait_for(cancelled.wait(), timeout=2)

    assert "poll_cancel_status" in called_methods
    assert "get_cancel_status" not in called_methods


@pytest.mark.asyncio
async def test_guard_poll_error_uses_one_shot_no_retries():
    """A transient error from poll_cancel_status must result in exactly one
    call per poll cycle — the guard retries on its own schedule, not the
    client's backoff loop. With the old get_cancel_status path, a
    TransportError triggered 4 retry attempts with backoff sleeps before
    the guard could move on.
    """
    call_count = {"n": 0}

    class _AlwaysFailingClient(FakeBackendClient):
        async def poll_cancel_status(self, task_id):
            call_count["n"] += 1
            raise ConnectionError("backend unreachable")

    client = _AlwaysFailingClient()
    with pytest.raises(asyncio.TimeoutError):
        async with CancelGuard(
            client, task_id=1, poll_interval_s=0.02,
        ) as cancelled:
            # Wait long enough for a few poll cycles, then time out.
            await asyncio.wait_for(cancelled.wait(), timeout=0.1)

    # Each poll cycle = exactly 1 call (no retries). With 0.02s interval
    # and 0.1s window, we expect ~3-5 calls. If _retry were active, each
    # cycle would fire 4 calls (max_retries=4), giving ~12-20.
    assert call_count["n"] <= 8, (
        f"expected ~one call per poll cycle, got {call_count['n']} — "
        "poll_cancel_status should not retry"
    )
    assert call_count["n"] >= 2, "expected at least 2 poll cycles"


@pytest.mark.asyncio
async def test_legacy_client_without_poll_cancel_status_still_detects_cancel():
    """A duck-typed client predating ``poll_cancel_status`` must not lose
    cancel detection. Before the fallback, every tick raised
    ``AttributeError`` on such a client; the poll loop swallows exceptions
    at DEBUG, so the guard silently never set its ``cancelled`` event —
    ``on_cancel`` (e.g. terminating a subprocess handler) never fired and
    the event threaded into prepare_inputs/upload_outputs never aborted a
    file transfer."""

    class _LegacyClient:
        """The pre-``poll_cancel_status`` protocol: only the retried poll."""

        def __init__(self):
            self.cancelled = False

        async def get_cancel_status(self, task_id):
            return {"cancelled": self.cancelled}

    client = _LegacyClient()
    client.cancelled = True
    on_cancel_fired = []

    with pytest.raises(TaskCancelled):
        async with CancelGuard(
            client, task_id=1, poll_interval_s=0.01,
            on_cancel=lambda: on_cancel_fired.append(True),
        ) as cancelled:
            await asyncio.wait_for(cancelled.wait(), timeout=2)

    assert on_cancel_fired == [True]


@pytest.mark.asyncio
async def test_legacy_fallback_warns_once_per_process(caplog):
    """The fallback condition is per-poll; one line per tick of every task
    would be noise, so the warning fires once per process (mirrors the
    ``report_progress_once`` fallback warning)."""

    class _LegacyClient:
        async def get_cancel_status(self, task_id):
            return {"cancelled": False}

    # The module-level flag is process-wide; a prior test may already have
    # tripped it, so reset it to make this test order-independent.
    cancel_mod._warned_legacy_cancel_client = False

    with caplog.at_level("WARNING"):
        for _ in range(3):
            async with CancelGuard(
                _LegacyClient(), task_id=1, poll_interval_s=0.01,
            ):
                await asyncio.sleep(0.03)

    assert sum(
        "poll_cancel_status" in r.message for r in caplog.records
    ) == 1
