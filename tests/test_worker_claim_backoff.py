"""Aggregate claim backoff for the poll loop.

``BackendClient._retry`` backs off *within* a single claim call, but that
schedule resets on every poll cycle: during a backend outage or deploy every
fleet worker re-hammered the struggling backend with a fresh 4-attempt retry
burst every ``poll_interval_s`` (~19s with the defaults), forever. Aggregate
load never decayed and a restarting backend got no recovery room.

``Worker`` now doubles the *idle wait between cycles* per consecutive claim
failure, capped at ``claim_backoff_max_s``, jittered ±25%, and reset by any
claim that reaches the backend. These tests pin: healthy polling is untouched,
the escalation doubles and caps, escalated waits are jittered (so a fleet
deployed together doesn't stay in lockstep through the outage), it collapses on
recovery, it can't overflow, degenerate pacing knobs are rejected at
construction, and shutdown still interrupts an escalated wait immediately.

Tests that assert on an exact schedule pass ``retry_jitter=False`` — the same
flag the BackendClient's retry tests use.
"""
from __future__ import annotations

import asyncio
import math

import pytest

from task_worker_api import TaskType
from task_worker_api.testing import FakeBackendClient


class _AlwaysFailingClient(FakeBackendClient):
    """``claim_next`` always raises, as if the backend were unreachable after
    the BackendClient's own retries were exhausted."""

    def __init__(self, *, stop_after: int = 0) -> None:
        super().__init__()
        self.claim_attempts = 0
        self._stop_after = stop_after
        self._worker = None

    def bind(self, worker) -> None:
        """Shut the worker down after ``stop_after`` claim attempts."""
        self._worker = worker

    async def claim_next(self, task_types, worker_id: str):
        self.claim_attempts += 1
        if self._worker is not None and self.claim_attempts >= self._stop_after:
            await self._worker.shutdown()
        raise RuntimeError("backend unreachable")


class _RecoveringClient(_AlwaysFailingClient):
    """Fails ``fail_count`` times, then answers normally (empty queue)."""

    def __init__(self, *, fail_count: int, stop_after: int) -> None:
        super().__init__(stop_after=stop_after)
        self._fail_count = fail_count

    async def claim_next(self, task_types, worker_id: str):
        self.claim_attempts += 1
        if self._worker is not None and self.claim_attempts >= self._stop_after:
            await self._worker.shutdown()
        if self.claim_attempts <= self._fail_count:
            raise RuntimeError("backend unreachable")
        return None


def _capture_wait_timeouts(monkeypatch) -> list[float]:
    """Capture the timeout that the poll loop actually passes to wait_for."""
    waits: list[float] = []

    async def _wait_for(awaitable, timeout):
        waits.append(timeout)
        awaitable.close()
        await asyncio.sleep(0)
        raise asyncio.TimeoutError

    monkeypatch.setattr("task_worker_api.worker.asyncio.wait_for", _wait_for)
    return waits


# ----- _claim_wait_s schedule ------------------------------------------------


def test_wait_is_poll_interval_while_healthy(make_worker, fake_client):
    worker = make_worker(client=fake_client, poll_interval_s=5.0)
    assert worker._claim_failures == 0
    assert worker._claim_wait_s() == 5.0


def test_wait_doubles_per_consecutive_failure(make_worker, fake_client):
    worker = make_worker(
        client=fake_client, poll_interval_s=5.0, claim_backoff_max_s=1000.0,
        retry_jitter=False,
    )
    for failures, expected in ((1, 10.0), (2, 20.0), (3, 40.0), (4, 80.0)):
        worker._claim_failures = failures
        assert worker._claim_wait_s() == expected


def test_wait_is_capped_at_claim_backoff_max_s(make_worker, fake_client):
    worker = make_worker(
        client=fake_client, poll_interval_s=5.0, claim_backoff_max_s=30.0,
        retry_jitter=False,
    )
    worker._claim_failures = 3  # would be 40s uncapped
    assert worker._claim_wait_s() == 30.0
    worker._claim_failures = 50
    assert worker._claim_wait_s() == 30.0


def test_default_claim_backoff_max_is_60s(make_worker, fake_client):
    """The documented default — 19s of poll interval must land on 60s, not
    grow without bound."""
    worker = make_worker(client=fake_client, retry_jitter=False)
    assert worker.claim_backoff_max_s == 60.0
    worker._claim_failures = 99
    assert worker._claim_wait_s() == 60.0


def test_cap_is_floored_at_poll_interval(make_worker, fake_client):
    """A cap below the poll interval must not make a failing worker poll
    *faster* than a healthy one."""
    worker = make_worker(
        client=fake_client, poll_interval_s=30.0, claim_backoff_max_s=5.0,
        retry_jitter=False,
    )
    assert worker._claim_wait_s() == 30.0
    worker._claim_failures = 4
    assert worker._claim_wait_s() == 30.0


def test_pathological_failure_count_stays_finite(make_worker, fake_client):
    """A worker left running for days against a dead backend accumulates a
    huge failure count. ``poll_interval_s * 2 ** failures`` would overflow to
    ``inf`` (and ``asyncio.wait_for(timeout=inf)`` would never poll again);
    iterative doubling stops at the cap. Jitter left on: it must not
    reintroduce a non-finite wait."""
    worker = make_worker(
        client=fake_client, poll_interval_s=5.0, claim_backoff_max_s=60.0,
    )
    worker._claim_failures = 100_000
    wait = worker._claim_wait_s()
    assert math.isfinite(wait)
    assert 45.0 <= wait <= 60.0  # 60s cap, negative jitter only at the cap


# ----- jitter ----------------------------------------------------------------


def test_escalated_wait_is_jittered_within_a_bounded_band(
    make_worker, fake_client,
):
    """Bounded means bounded: every sample sits inside the ±25% band around
    the deterministic delay, and the values actually vary."""
    worker = make_worker(
        client=fake_client, poll_interval_s=5.0, claim_backoff_max_s=1000.0,
    )
    worker._claim_failures = 3  # deterministic delay: 40s
    samples = [worker._claim_wait_s() for _ in range(50)]

    assert all(30.0 <= s <= 50.0 for s in samples)
    assert len(set(samples)) > 1


def test_jitter_decorrelates_workers_with_identical_config(
    make_worker, fake_client,
):
    """The thundering-herd regression: the fleet rolls as one deploy, so its
    workers hit an outage with identical config and identical failure counts.
    Without jitter their Nth backoff expires on the same instant and the
    recovering backend takes the whole fleet's burst at once."""
    def _schedule():
        worker = make_worker(
            client=fake_client, poll_interval_s=5.0,
            claim_backoff_max_s=1000.0,
        )
        waits = []
        for failures in range(1, 8):
            worker._claim_failures = failures
            waits.append(worker._claim_wait_s())
        return waits

    assert _schedule() != _schedule()


def test_jittered_wait_never_escapes_the_cap_or_the_poll_interval(
    make_worker, fake_client,
):
    """Jitter is applied *inside* ``[poll_interval_s, cap]``, not around it.

    Jittering a delay that has already been capped would let an escalated wait
    overshoot ``claim_backoff_max_s`` by 25% — and, worse, undershoot
    ``poll_interval_s`` at the low end. Both bounds are pinned here with jitter
    on, at a failure count deep past the cap so every sample sits on it.
    """
    worker = make_worker(
        client=fake_client, poll_interval_s=5.0, claim_backoff_max_s=30.0,
    )
    worker._claim_failures = 20  # far past the cap
    samples = [worker._claim_wait_s() for _ in range(200)]

    assert all(5.0 <= s <= 30.0 for s in samples)
    assert len(set(samples)) > 1  # still decorrelated


def test_jitter_at_cap_samples_the_bounded_band_without_clipping(
    make_worker, fake_client, monkeypatch,
):
    """Sampling the legal band directly avoids piling half the fleet onto
    the exact cap when a symmetric sample would have exceeded it."""
    bounds = []

    def midpoint(low, high):
        bounds.append((low, high))
        return (low + high) / 2

    monkeypatch.setattr("task_worker_api.worker.random.uniform", midpoint)
    worker = make_worker(
        client=fake_client, poll_interval_s=5.0, claim_backoff_max_s=30.0,
    )
    worker._claim_failures = 20

    assert worker._claim_wait_s() == 26.25
    assert bounds == [(22.5, 30.0)]


def test_failing_worker_never_polls_faster_than_a_healthy_one_with_jitter(
    make_worker, fake_client,
):
    """The floor holds under jitter, not just in the deterministic schedule.

    With ``claim_backoff_max_s <= poll_interval_s`` the cap collapses onto the
    poll interval, so negative jitter on the capped delay would make a *failing*
    worker poll up to 25% faster than a healthy one — hammering the very backend
    it is supposed to be backing off from. Clamping after jitter pins it.
    """
    worker = make_worker(
        client=fake_client, poll_interval_s=30.0, claim_backoff_max_s=5.0,
    )
    healthy = worker._claim_wait_s()
    for failures in (1, 2, 7, 50):
        worker._claim_failures = failures
        assert all(
            worker._claim_wait_s() >= healthy for _ in range(50)
        ), f"failing worker polled faster than healthy at {failures} failures"


def test_healthy_wait_is_never_jittered(make_worker, fake_client):
    """Jitter exists to break up a herd of *failing* workers. A worker whose
    claims land has no herd to break up, and consumers pace real work on
    ``poll_interval_s``, so the healthy wait stays exact."""
    worker = make_worker(client=fake_client, poll_interval_s=5.0)
    assert {worker._claim_wait_s() for _ in range(20)} == {5.0}


# ----- pacing knob validation ------------------------------------------------


_PACING_KNOBS = [
    "poll_interval_s",
    "claim_backoff_max_s",
    "heartbeat_interval_s",
    "cancel_poll_interval_s",
    "timeout_grace_s",
]


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf")])
@pytest.mark.parametrize("knob", _PACING_KNOBS)
def test_degenerate_pacing_knobs_are_rejected(make_worker, fake_client, knob, bad):
    """Every pacing knob fails the same two ways, silently at construction and
    expensively in production against the one backend the fleet shares.

    Too fast: zero/negative (and ``NaN``, which fails the same comparisons)
    spins the claim loop, the heartbeat's ``PUT /progress`` and the cancel
    guard's ``GET /cancel-status`` at full speed, and collapses the watchdog's
    SIGTERM → grace → SIGKILL → grace escalation into an immediate hard exit.
    Too slow: ``inf`` makes the first idle wait never end, never heartbeats,
    never notices a cancel, and never reaches the hard exit."""
    with pytest.raises(ValueError, match=knob):
        make_worker(client=fake_client, **{knob: bad})


@pytest.mark.parametrize("knob", _PACING_KNOBS)
def test_valid_pacing_knobs_are_coerced_to_float(make_worker, fake_client, knob):
    """Ints are a normal way to spell these (``int(os.environ[...])``) and must
    keep working."""
    worker = make_worker(client=fake_client, **{knob: 7})
    assert getattr(worker, knob) == 7.0
    assert type(getattr(worker, knob)) is float


# ----- _claim failure bookkeeping --------------------------------------------


@pytest.mark.asyncio
async def test_claim_failure_increments_counter(make_worker):
    worker = make_worker(client=_AlwaysFailingClient())
    assert await worker._claim() is None
    assert worker._claim_failures == 1
    assert await worker._claim() is None
    assert worker._claim_failures == 2


@pytest.mark.asyncio
async def test_empty_queue_resets_counter(make_worker, fake_client):
    """An empty queue is a *successful* round-trip — the backend answered —
    so it must collapse the escalation, not sustain it."""
    worker = make_worker(client=fake_client)
    worker._claim_failures = 4
    assert await worker._claim() is None
    assert worker._claim_failures == 0


@pytest.mark.asyncio
async def test_successful_claim_resets_counter(
    make_worker, fake_client, queue_cut_planes_task,
):
    queue_cut_planes_task()
    worker = make_worker(client=fake_client)
    worker._claim_failures = 3
    assert await worker._claim() is not None
    assert worker._claim_failures == 0


# ----- run_forever integration -----------------------------------------------


@pytest.mark.asyncio
async def test_healthy_poll_loop_waits_exactly_poll_interval(
    make_worker, fake_client, monkeypatch,
):
    """No failures → no escalation. The idle wait stays exactly
    ``poll_interval_s`` so healthy polling behaviour is unchanged."""
    worker = make_worker(client=fake_client, poll_interval_s=7.0)
    waits = _capture_wait_timeouts(monkeypatch)

    async def stop_after_cycles():
        while len(waits) < 4:
            await asyncio.sleep(0)
        await worker.shutdown()

    await asyncio.gather(worker.run_forever(), stop_after_cycles())

    assert waits[:4] == [7.0, 7.0, 7.0, 7.0]


@pytest.mark.asyncio
async def test_poll_loop_backs_off_exponentially_during_outage(
    make_worker, monkeypatch,
):
    client = _AlwaysFailingClient(stop_after=5)
    worker = make_worker(
        client=client, poll_interval_s=5.0, claim_backoff_max_s=1000.0,
        retry_jitter=False,
    )
    client.bind(worker)
    waits = _capture_wait_timeouts(monkeypatch)

    await worker.run_forever()

    # Cycle N's wait reflects N consecutive failures: 10, 20, 40, 80, 160.
    assert waits == [10.0, 20.0, 40.0, 80.0, 160.0]


@pytest.mark.asyncio
async def test_poll_loop_backoff_saturates_at_cap(make_worker, monkeypatch):
    client = _AlwaysFailingClient(stop_after=6)
    worker = make_worker(
        client=client, poll_interval_s=5.0, claim_backoff_max_s=30.0,
        retry_jitter=False,
    )
    client.bind(worker)
    waits = _capture_wait_timeouts(monkeypatch)

    await worker.run_forever()

    assert waits == [10.0, 20.0, 30.0, 30.0, 30.0, 30.0]


@pytest.mark.asyncio
async def test_poll_loop_backoff_collapses_after_recovery(make_worker, monkeypatch):
    """The whole point of resetting on a successful round-trip: a backend that
    comes back must be polled at the normal interval again immediately, not
    stay starved for a minute."""
    client = _RecoveringClient(fail_count=3, stop_after=6)
    worker = make_worker(
        client=client, poll_interval_s=5.0, claim_backoff_max_s=1000.0,
        retry_jitter=False,
    )
    client.bind(worker)
    waits = _capture_wait_timeouts(monkeypatch)

    await worker.run_forever()

    assert waits == [10.0, 20.0, 40.0, 5.0, 5.0, 5.0]


@pytest.mark.asyncio
async def test_shutdown_interrupts_an_escalated_wait(make_worker):
    """The escalated wait observes ``_stop`` rather than sleeping, so a
    shutdown during a 60s backoff exits now — not up to a minute later."""
    client = _AlwaysFailingClient()
    worker = make_worker(
        client=client, poll_interval_s=30.0, claim_backoff_max_s=600.0,
        retry_jitter=False,
    )
    worker._claim_failures = 6  # next wait would be the 600s cap
    assert worker._claim_wait_s() == 600.0

    run = asyncio.ensure_future(worker.run_forever())
    while client.claim_attempts < 1:
        await asyncio.sleep(0)
    await worker.shutdown()
    # No wait_for(timeout=...) shortcut here: if the loop slept instead of
    # waiting on the event, this await would hang for the full 600s.
    await asyncio.wait_for(run, timeout=5.0)

    assert run.done()


@pytest.mark.asyncio
async def test_backoff_is_logged_once_per_escalated_cycle(make_worker, caplog):
    client = _AlwaysFailingClient(stop_after=3)
    # Real (unscaled) waits here so the logged delay is the one actually
    # slept: 0.1 + 0.2 + 0.4s. Jitter off so those numbers are exact.
    worker = make_worker(
        client=client, poll_interval_s=0.05, claim_backoff_max_s=1000.0,
        retry_jitter=False,
    )
    client.bind(worker)

    with caplog.at_level("INFO"):
        await worker.run_forever()

    backoff_logs = [
        r.getMessage() for r in caplog.records if "backing off" in r.getMessage()
    ]
    assert len(backoff_logs) == 3
    assert "1 consecutive times; backing off 0.1s" in backoff_logs[0]
    assert "3 consecutive times; backing off 0.4s" in backoff_logs[2]
    # The per-failure WARNING carries the consecutive count for operators.
    assert any(
        "3 consecutive" in r.getMessage()
        for r in caplog.records
        if r.levelname == "WARNING"
    )


@pytest.mark.asyncio
async def test_no_backoff_log_while_healthy(
    make_worker, fake_client, caplog, monkeypatch,
):
    worker = make_worker(client=fake_client, poll_interval_s=5.0)
    waits = _capture_wait_timeouts(monkeypatch)

    async def stop_after_cycles():
        while len(waits) < 3:
            await asyncio.sleep(0)
        await worker.shutdown()

    with caplog.at_level("INFO"):
        await asyncio.gather(worker.run_forever(), stop_after_cycles())

    assert not [r for r in caplog.records if "backing off" in r.getMessage()]


@pytest.mark.asyncio
async def test_processed_task_between_failures_resets_the_escalation(
    make_worker, tmp_path, monkeypatch,
):
    """A worker that fails a few claims, then successfully claims and *runs* a
    task, is talking to a healthy backend — the next idle wait is the plain
    poll interval."""
    stl = tmp_path / "fake.stl"
    stl.write_bytes(b"solid\nendsolid\n")

    class _FlakyThenWorking(_AlwaysFailingClient):
        async def claim_next(self, task_types, worker_id: str):
            self.claim_attempts += 1
            if self.claim_attempts <= 2:
                raise RuntimeError("backend unreachable")
            if self._worker is not None and self.claim_attempts >= self._stop_after:
                await self._worker.shutdown()
            # Cycle 3 pops the queued task; later cycles find an empty queue.
            return await FakeBackendClient.claim_next(self, task_types, worker_id)

    client = _FlakyThenWorking(stop_after=5)
    client.queue_task(
        task_type=TaskType.DETECT_CUT_PLANES,
        params={"input_path": str(stl)},
    )

    async def handler(ctx, params):
        return {"planes": []}

    worker = make_worker(
        client=client,
        handlers={TaskType.DETECT_CUT_PLANES: handler},
        poll_interval_s=5.0,
        claim_backoff_max_s=1000.0,
        retry_jitter=False,
    )
    client.bind(worker)
    waits = _capture_wait_timeouts(monkeypatch)

    await worker.run_forever()

    # Cycles 1-2 escalate; cycle 3 claims and runs a task (no idle wait at
    # all); cycles 4-5 idle at the plain interval.
    assert waits == [10.0, 20.0, 5.0, 5.0]
    assert len(client.completed_tasks) == 1
