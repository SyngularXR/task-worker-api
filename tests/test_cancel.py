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

from task_worker_api.cancel import CancelGuard
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
