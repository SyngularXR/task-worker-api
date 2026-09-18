import asyncio
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from task_worker_api.errors import ProtocolError
from task_worker_api.resource_execution import AttemptLease
from task_worker_api.resource_protocol import AttemptState


class Client:
    def __init__(self, lease_seconds=30):
        self.claim = SimpleNamespace(task_id=1, ownership=SimpleNamespace(attempt_id=uuid4()),
                                     staging_deadline=datetime.now(timezone.utc) + timedelta(seconds=60))
        self.seconds = lease_seconds
        self.state = "reserved"

    def response(self):
        now = datetime.now(timezone.utc)
        return AttemptState(task_id=1, attempt_id=self.claim.ownership.attempt_id,
            state=self.state, task_status=1, cancelled=False, server_time=now,
            lease_expires_at=now + timedelta(seconds=self.seconds), progress={},
            execution_deadline=now + timedelta(seconds=10) if self.state == "running" else None)

    async def resource_status(self, claim):
        return self.response()

    async def resource_operation(self, journal, kind, payload, **kwargs):
        assert kind == "start" and payload == {"input_digest": "staged-inputs"}
        self.state = "running"
        return self.response()


def test_delayed_renewal_preserves_acknowledged_time_without_crossing_phase_deadline(monkeypatch):
    monkeypatch.setattr('task_worker_api.resource_execution.time.monotonic', lambda: 100.0)
    client = Client(lease_seconds=10)
    lease = AttemptLease(client, None, client.claim)
    lease._deadline = 108.0
    lease._accept(client.response(), 95.0)
    assert lease._deadline == 108.0
    client.state = 'running'
    lease._execution_deadline = 104.0
    lease._accept(client.response(), 95.0)
    assert lease._deadline == 104.0
    monkeypatch.setattr('task_worker_api.resource_execution.time.monotonic', lambda: 105.0)
    with pytest.raises(ProtocolError, match='expired'):
        lease._accept(client.response(), 105.0)


@pytest.mark.asyncio
async def test_renewal_wait_scales_with_remaining_acknowledged_time(monkeypatch):
    monkeypatch.setattr('task_worker_api.resource_execution.time.monotonic', lambda: 100.0)
    client = Client()
    lease = AttemptLease(client, None, client.claim)
    lease._deadline = 108.0
    waits = []
    async def sleep(delay):
        waits.append(delay)
        raise asyncio.CancelledError
    monkeypatch.setattr('task_worker_api.resource_execution.asyncio.sleep', sleep)
    with pytest.raises(asyncio.CancelledError):
        await lease._renew()
    assert waits == [2.0]

@pytest.mark.asyncio
async def test_compute_requires_start_and_cannot_reuse_closed_lease():
    client = Client()
    calls = []

    async def compute():
        calls.append("compute")
        return 42

    lease = AttemptLease(client, None, client.claim)
    async with lease:
        with pytest.raises(ProtocolError, match="live acknowledged start"):
            await lease.run(compute)
        assert not calls
        await lease.start(None, "staged-inputs")
        assert await lease.run(compute) == 42
    with pytest.raises(ProtocolError):
        await lease.run(compute)
    with pytest.raises(ProtocolError):
        async with lease:
            pass
    assert calls == ["compute"]


@pytest.mark.asyncio
async def test_recovered_running_attempt_cannot_execute():
    client = Client()
    client.state = "running"
    with pytest.raises(ProtocolError, match="reconciliation"):
        async with AttemptLease(client, None, client.claim):
            pytest.fail("must not enter staging or computation")


@pytest.mark.asyncio
async def test_expiry_cancels_work_and_hard_exits_when_loop_is_blocked():
    client = Client(lease_seconds=0.1)
    exited = threading.Event()

    async def blocked():
        async with AttemptLease(client, None, client.claim, grace_s=0.1, on_hard_exit=exited.set) as lease:
            await lease.start(None, "staged-inputs")

            async def compute():
                # Deliberately freeze the event loop; the watchdog must still fire.
                assert exited.wait(2)
                return "late result"

            await lease.run(compute)

    task = asyncio.create_task(blocked())
    with pytest.raises((ProtocolError, asyncio.CancelledError)):
        await task
    assert exited.is_set()


@pytest.mark.asyncio
async def test_delayed_or_wrong_attempt_response_cannot_extend_lease():
    client = Client(lease_seconds=0.1)
    lease = AttemptLease(client, None, client.claim)
    with pytest.raises(ProtocolError, match="expired"):
        lease._accept(client.response(), time.monotonic() - 1)
    wrong = client.response().model_copy(update={"attempt_id": uuid4()})
    with pytest.raises(ProtocolError, match="another attempt"):
        lease._accept(wrong, time.monotonic())


@pytest.mark.asyncio
@pytest.mark.parametrize("revoked", [False, True])
async def test_heartbeat_failure_cannot_keep_work_alive(revoked):
    client = Client(lease_seconds=5.2)
    renewed = asyncio.Event()
    exited = threading.Event()

    async def heartbeat(claim):
        renewed.set()
        if revoked:
            return client.response().model_copy(update={"cancelled": True})
        await asyncio.Future()  # transport retry never returns

    client.resource_heartbeat = heartbeat

    async def work():
        async with AttemptLease(client, None, client.claim, on_hard_exit=exited.set) as lease:
            await lease.start(None, "staged-inputs")
            await lease.run(asyncio.Event().wait)

    task = asyncio.create_task(work())
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 7)
    assert renewed.is_set() and not exited.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("degraded", ["parks", "fails"])
async def test_renewal_cannot_spend_the_whole_lease_on_one_retried_heartbeat(degraded):
    """A renewal keeps the client's retry policy, but never for longer than the
    lease it is renewing.

    Unbounded, one 429's ``Retry-After`` parks that retry loop for the hour it
    names — the v2 path never shortens server guidance — so the renewal comes
    back to an expired lease: ``_watch`` has cancelled the owner task mid-work
    and hard-exited the worker. Bounding it at the *whole* remaining lease is no
    fix either, which is why the allowance is only half: spend it all on one
    attempt that never answers (``parks``) and the loop is handed back no time
    to try again with, so the lease dies just the same.

    Asserted on what a single bounded call cannot show: whichever way the first
    renewal fails, a later one lands *before* expiry and the owner runs on
    untouched past the lease it started with."""
    client = Client(lease_seconds=3)
    exited = threading.Event()
    shots = []

    async def heartbeat(claim):
        shots.append(time.monotonic())
        if len(shots) == 1:
            if degraded == "parks":
                await asyncio.sleep(3600)  # the client's retry loop, parked on Retry-After
            raise RuntimeError("degraded backend")
        return client.response()

    client.resource_heartbeat = heartbeat
    lease = AttemptLease(client, None, client.claim, grace_s=0.1, on_hard_exit=exited.set)

    async def owner():
        async with lease:
            await asyncio.sleep(3.5)

    task = asyncio.create_task(owner())
    done, _ = await asyncio.wait([task], timeout=8)
    assert task in done and not task.cancelled(), \
        "the watchdog cancelled the owner mid-work: no renewal landed inside the 3s lease"
    task.result()
    assert len(shots) >= 2 and not exited.is_set()


@pytest.mark.asyncio
async def test_entry_poll_cannot_outlive_the_runway_it_could_be_granted(monkeypatch):
    """Entry is the other lease-bound poll, and the one with no acknowledged
    deadline yet: parked on a server-named retry it returns to a reservation the
    backend has already reclaimed. Its bound cannot come from
    ``claim.lease_expires_at`` minus local wall time — that reads the worker's
    clock error as lease time — so it is _accept's own cap, which dates every
    acknowledgement from the monotonic send time and grants at most
    _MAX_LEASE_RUNWAY_S past it. A reply later than that is rejected anyway.

    Asserted on elapsed time: a per-request timeout is not an end-to-end bound
    (httpcore restarts the read deadline on every chunk), and the park this
    interrupts happens between attempts, where no request timeout reaches."""
    monkeypatch.setattr('task_worker_api.resource_execution._MAX_LEASE_RUNWAY_S', 0.2)
    client = Client()

    async def parked(claim):
        await asyncio.sleep(5)
        return client.response()

    client.resource_status = parked
    started = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        async with AttemptLease(client, None, client.claim):
            pass
    assert time.monotonic() - started < 1.0, "entry outlived the runway it could be granted"


@pytest.mark.asyncio
async def test_leaving_the_lease_stops_renewal_even_if_its_cancel_is_swallowed():
    """``asyncio.wait_for`` on Python <= 3.11 swallows a cancel that lands in the
    loop pass its inner call completes in (fixed in 3.12; the SDK supports 3.10).
    An owner woken by the heartbeat's own completion leaves the lease in exactly
    that pass: ``__aexit__`` cancels ``_renew``, the cancel is eaten, and the loop
    renews on while ``__aexit__`` waits for it — until the phase deadline, 60s
    here. The loop has to notice the exit itself.

    Passes on 3.12+ with or without that check; on 3.11 it fails without it."""
    client = Client(lease_seconds=2)
    exited = threading.Event()
    leave = asyncio.Event()
    shots = []

    async def heartbeat(claim):
        shots.append(time.monotonic())
        leave.set()  # wakes the owner in the pass this call completes in
        return client.response()

    client.resource_heartbeat = heartbeat
    lease = AttemptLease(client, None, client.claim, on_hard_exit=exited.set)

    async def owner():
        async with lease:
            await leave.wait()

    task = asyncio.create_task(owner())
    done, _ = await asyncio.wait([task], timeout=1.5)
    try:
        assert task in done, "__aexit__ is still waiting on a renewal loop that outlived its cancel"
        assert len(shots) == 1 and not exited.is_set()
    finally:
        lease._heartbeat.cancel()
        await asyncio.wait([task], timeout=1)
