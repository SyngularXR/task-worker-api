"""Lease control for one admitted attempt; no cleanup or release assertions.

Enter before staging and keep open through publication and cleanup. Call start only after
staging; run invokes compute only after that acknowledgement. On restart, recover
the journal with the supervisor instead of entering an old running attempt.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import threading
import time

from .errors import ProtocolError

log = logging.getLogger(__name__)

# The most monotonic runway any single acknowledgement can grant, however long
# a lease the backend names. _accept clamps to it, so it is also the longest an
# entry poll can usefully run for — past it no reply yields a usable lease.
_MAX_LEASE_RUNWAY_S = 30


class AttemptLease:
    def __init__(self, client, journal, claim, *, grace_s=10, on_hard_exit=None):
        if not math.isfinite(grace_s) or grace_s <= 0:
            raise ValueError("cleanup grace must be positive")
        self.client, self.journal, self.claim = client, journal, claim
        self.grace_s = grace_s
        self._exit = on_hard_exit or (lambda: os._exit(75))
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._expired = threading.Event()
        self._deadline = 0.0
        self._execution_deadline = None
        self._started = False
        self._entered = False
        self._active = False

    def _accept(self, state, sent):
        if state.task_id != self.claim.task_id or state.attempt_id != self.claim.ownership.attempt_id:
            raise ProtocolError("lease response belongs to another attempt")
        if state.cancelled or state.state not in ("reserved", "running", "releasing"):
            raise ProtocolError("attempt is no longer executable")
        # Request-send time subtracts all transport latency and avoids trusting
        # the worker wall clock. A delayed response cannot extend an expired lease.
        deadline = sent + min(_MAX_LEASE_RUNWAY_S, (state.lease_expires_at - state.server_time).total_seconds())
        phase = state.execution_deadline or self.claim.staging_deadline
        with self._lock:
            if self._expired.is_set() or (self._deadline and time.monotonic() >= self._deadline):
                raise ProtocolError("attempt lease expired")
            # A slow renewal cannot revoke time already acknowledged by this
            # authority. Phase limits still cap it; an expired lease stays expired.
            deadline = max(self._deadline, deadline)
            if state.state != "releasing":
                deadline = min(deadline, sent + (phase - state.server_time).total_seconds())
                if self._execution_deadline is not None:
                    deadline = min(deadline, self._execution_deadline)
            if deadline <= time.monotonic():
                raise ProtocolError("attempt lease expired")
            self._deadline = deadline
        return state

    async def __aenter__(self):
        if self._entered:
            raise ProtocolError("attempt lease cannot be reused")
        self._entered = True
        self._loop = asyncio.get_running_loop()
        self._owner = asyncio.current_task()
        sent = time.monotonic()
        # Unbounded, one 429's Retry-After parks entry until the reservation has
        # been reclaimed. Entry has no acknowledged deadline yet, and
        # claim.lease_expires_at cannot supply one — subtracting local wall time
        # from it reads worker clock error as lease time. _accept's own cap is
        # the bound with no clock to trust: a reply later than that is rejected
        # anyway. Retries stay on; unlike _renew there is no next pass to save
        # time for, so giving up here is the outcome rather than a deferral.
        state = self._accept(await asyncio.wait_for(
            self.client.resource_status(self.claim), _MAX_LEASE_RUNWAY_S), sent)
        if state.state != "reserved":
            raise ProtocolError("previous execution requires supervisor reconciliation")
        threading.Thread(target=self._watch, name="attempt-lease", daemon=True).start()
        self._heartbeat = asyncio.create_task(self._renew())
        self._active = True
        return self

    def _watch(self):
        while not self._closed.wait(0.05):
            with self._lock:
                expired = time.monotonic() >= self._deadline
                if expired:
                    self._expired.set()
            if expired:
                with contextlib.suppress(RuntimeError):
                    self._loop.call_soon_threadsafe(self._owner.cancel)
                # No network call or process enumeration can delay hard exit.
                # The trusted supervisor still owns descendant cleanup evidence.
                if not self._closed.wait(self.grace_s):
                    self._exit()
                return

    async def _renew(self):
        while True:
            with self._lock:
                remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(min(5, remaining / 4))
            sent = time.monotonic()
            with self._lock:
                # The renewal keeps the client's retry policy — riding out a
                # blip in place is what a heartbeat wants — but not for longer
                # than the lease it is renewing: unbounded, one 429's
                # Retry-After parks it for the hour it names (the v2 path never
                # shortens server guidance) and it returns to an expired lease,
                # an owner task cancelled mid-work and a hard-exited worker.
                #
                # Half of what the lease has left, off the monotonic deadline
                # _accept built from the backend's own server_time deltas. Half,
                # so one stalled attempt cannot spend the whole lease: the rest
                # is this loop's, to sleep its cadence and renew inside the
                # acknowledged window. Elapsed, because a per-request timeout is
                # not an end-to-end bound — it neither spans the retry loop's
                # inter-attempt sleeps nor survives a dribbling peer, since
                # httpcore restarts the read deadline on every chunk.
                allowance = (self._deadline - sent) / 2
            try:
                self._accept(await asyncio.wait_for(
                    self.client.resource_heartbeat(self.claim), allowance), sent)
            except ProtocolError:
                with self._lock:
                    self._expired.set()
                    self._deadline = 0
                return
            except Exception:
                log.warning("Attempt heartbeat failed; acknowledged lease still expires", exc_info=True)
            if not self._active:
                # __aexit__ cancelled this task and the cancel never arrived:
                # wait_for on Python <= 3.11 swallows one that lands in the
                # loop pass its inner call completes in. Unchecked, the loop
                # renews on and __aexit__ waits on it until the phase deadline.
                return

    async def start(self, host_report, input_digest):
        if not self._active or self._started or self._closed.is_set():
            raise ProtocolError("attempt already started or closed")
        sent = time.monotonic()
        state = self._accept(await self.client.resource_operation(
            self.journal, "start", {"input_digest": input_digest}, host_report=host_report), sent)
        if state.state != "running" or state.execution_deadline is None:
            raise ProtocolError("start did not authorize execution")
        with self._lock:
            self._execution_deadline = sent + (state.execution_deadline - state.server_time).total_seconds()
            self._started = True

    async def run(self, handler, *args):
        self._require_running()
        result = await handler(*args)
        self._require_running()
        return result

    @property
    def is_cancelled(self):
        return self._expired.is_set() or self._closed.is_set() or time.monotonic() >= self._deadline

    def raise_if_cancelled(self):
        if self.is_cancelled:
            from .errors import TaskCancelled
            raise TaskCancelled("attempt lease no longer permits work")

    async def update(self, stage, current=0, total=0):
        sent = time.monotonic()
        try:
            self._accept(await self.client.resource_progress(self.claim,
                {"stage": stage, "current": current, "total": total}), sent)
        except ProtocolError:
            self._expired.set()
            with self._lock:
                self._deadline = 0
            raise
        except Exception:
            log.warning("Attempt progress update failed", exc_info=True)

    def _require_running(self):
        with self._lock:
            if not self._started or self._closed.is_set() or self._expired.is_set() or time.monotonic() >= self._deadline:
                raise ProtocolError("execution requires a live acknowledged start")

    async def __aexit__(self, *exc):
        self._active = False
        self._heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._heartbeat
        self._closed.set()
