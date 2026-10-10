"""Local resident lease fence. Stopping owned resources never releases the ledger.

Callbacks are trusted supervisor operations, never engine acknowledgements.
Withdraw closes routing immediately; stop/force-stop run outside the event loop.
Persist their actual cleanup outcomes before reconciling release with the authority.
"""
import asyncio
import contextlib
import threading
import time

import httpx

from .client import _rejection_code
from .errors import ProtocolError
from .resource_execution import _MAX_LEASE_RUNWAY_S
from .resources import CleanupEvidence
from .service_protocol import validate_service_state


class ResidentServiceLease:
    def __init__(self, client, grant, read_report, on_withdraw, stop_owned, force_stop_owned,
                 *, operation_id_factory):
        self.client, self.grant, self.read_report = client, grant, read_report
        self.on_withdraw, self.stop_owned, self.force_stop_owned = on_withdraw, stop_owned, force_stop_owned
        self.operation_id_factory = operation_id_factory
        self._lock = threading.Lock()
        self._lost, self._cleanup_done = threading.Event(), threading.Event()
        self._reclaim_done = threading.Event()
        self._deadline = self._residence = 0.0
        self._phase = 'warming'
        self._entered = self._active = False
        self.cleanup_result = self.force_cleanup_result = None
        self.cleanup_error = None
        self.lease_expires_at = grant.lease_expires_at

    def _lose(self):
        with self._lock:
            if self._lost.is_set():
                return
            self._lost.set()
        def stop():
            try:
                self.on_withdraw()
                self.cleanup_result = self.stop_owned()
            except BaseException:
                self.cleanup_error = 'owned_stop_failed'
            finally:
                self._cleanup_done.set()
        def reclaim():
            try:
                threading.Thread(target=stop, daemon=True).start()
                if not self._cleanup_done.wait(self.grant.profile.reclaim_timeout_seconds) or self.cleanup_error:
                    try:
                        self.force_cleanup_result = self.force_stop_owned()
                    except BaseException:
                        self.cleanup_error = 'owned_force_stop_failed'
            finally:
                self._reclaim_done.set()
        threading.Thread(target=reclaim, daemon=True).start()

    def _accept(self, state, sent):
        try:
            validate_service_state(self.grant, state)
            if state.state not in ('warming', 'idle', 'serving'):
                raise ProtocolError('service grant withdrawn')
            deadline = sent + min(_MAX_LEASE_RUNWAY_S,
                                  (state.lease_expires_at-state.server_time).total_seconds())
            residence = sent + (state.residence_deadline-state.server_time).total_seconds()
            with self._lock:
                now = time.monotonic()
                if self._lost.is_set() or (self._deadline and now >= self._deadline):
                    raise ProtocolError('service lease expired')
                self._residence = min(self._residence or residence, residence)
                deadline = min(max(self._deadline, deadline), self._residence)
                if deadline <= now:
                    raise ProtocolError('service lease expired')
                self._deadline, self._phase = deadline, state.state
                self.lease_expires_at = state.lease_expires_at
            return state
        except ProtocolError:
            self._lose()
            raise

    def require_live(self):
        with self._lock:
            live = self._active and not self._lost.is_set() and time.monotonic() < self._deadline
        if not live:
            raise ProtocolError('service requires a live acknowledged grant')

    @property
    def can_dispatch(self):
        with self._lock:
            return (self._active and not self._lost.is_set() and time.monotonic() < self._deadline
                    and self._phase in ('idle', 'serving'))

    def require_dispatch(self):
        if not self.can_dispatch:
            raise ProtocolError('service is not ready for dispatch')

    @property
    def remaining_seconds(self):
        with self._lock:
            return max(0, self._deadline-time.monotonic()) if not self._lost.is_set() else 0

    def withdraw(self):
        self._lose()

    def close(self):
        self._active = False
        self._lose()

    def wait_stopped(self):
        """Bounded local wait; None is unknown, never proof of ledger release."""
        self._reclaim_done.wait(self.grant.profile.reclaim_timeout_seconds)
        result = self.force_cleanup_result or self.cleanup_result
        if (isinstance(result, CleanupEvidence) and result.attempt_id == self.grant.ownership.attempt_id
                and result.boot_id == self.grant.boot_id and result.processes_stopped
                and result.models_evicted and result.scratch_cleaned):
            return result
        return None

    def _watch(self):
        while not self._lost.wait(0.05):
            with self._lock:
                expired = time.monotonic() >= self._deadline
            if expired:
                self._lose()
                return

    async def __aenter__(self):
        if self._entered or self.grant.state != 'warming':
            raise ProtocolError('recovered service requires owned cleanup, never resume')
        self._entered = True
        sent = time.monotonic()
        state = await asyncio.wait_for(self.client.service_status(self.grant), _MAX_LEASE_RUNWAY_S)
        if state.state != 'warming':
            raise ProtocolError('recovered service requires owned cleanup, never resume')
        self._accept(state, sent)
        self._active = True
        threading.Thread(target=self._watch, daemon=True).start()
        self._heartbeat = asyncio.create_task(self._renew())
        return self

    async def ready(self, operation_id, host_report):
        self.require_live()
        sent = time.monotonic()
        try:
            state = await asyncio.wait_for(self.client.service_ready(self.grant, operation_id, host_report),
                                           max(0, (self._deadline-sent)/2))
            return self._accept(state, sent)
        except BaseException:
            self._lose()
            raise

    async def _renew(self):
        while self._active and not self._lost.is_set():
            remaining = self._deadline-time.monotonic()
            if remaining <= 0:
                self._lose()
                return
            await asyncio.sleep(min(5, remaining/4))
            sent = time.monotonic()
            try:
                async def renew():
                    operation_id = self.operation_id_factory('renew')
                    return await self.client.service_renew(self.grant, operation_id, await self.read_report())
                state = await asyncio.wait_for(renew(), max(0, (self._deadline-sent)/2))
                self._accept(state, sent)
            except ProtocolError:
                self._lose()
                return
            except httpx.HTTPStatusError as exc:
                if _rejection_code(exc) == 'attempt_fenced':
                    self._lose()
                    return
            except Exception:
                pass  # A transient failure cannot extend acknowledged local ownership.

    async def __aexit__(self, *exc):
        self.close()
        self._heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._heartbeat
