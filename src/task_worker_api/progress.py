"""Progress reporter with a background heartbeat loop.

The heartbeat serves two purposes:
1. Keep the Task row's ``updated_at`` fresh so the sweeper doesn't mark
   it stale.
2. Observe cancel signals — if ``report_progress`` returns
   ``{cancelled: true}``, we set an asyncio.Event that handlers + the
   CancelGuard can watch.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .client import BackendClient

log = logging.getLogger(__name__)

_REMOTE_KILL_HANDLE = {"pid": None, "container": None, "host": "remote"}

#: Set once a client without ``report_progress_once`` has been warned about,
#: so a legacy client logs one WARNING per process rather than one per stage
#: transition of every task the worker runs.
_warned_legacy_progress_client = False


def _one_shot_report(client: "BackendClient"):
    """``client.report_progress_once`` if it has one, else ``report_progress``.

    ``report_progress_once`` was added after the worker repos had already
    grown their own clients, test doubles and ``FakeBackendClient`` subclasses
    against the older protocol (``Worker(client=...)`` takes any duck-typed
    client). Calling it unconditionally would raise ``AttributeError`` on
    every one of them at the first stage transition, so a client that lacks it
    keeps the retried call it always had — it just keeps the old worst case,
    where one immediate report can block the handler for ``max_retries`` ×
    ``lifecycle_timeout_s`` plus backoff.
    """
    report = getattr(client, "report_progress_once", None)
    if report is not None:
        return report
    global _warned_legacy_progress_client
    if not _warned_legacy_progress_client:
        _warned_legacy_progress_client = True
        log.warning(
            "%s has no 'report_progress_once'; an immediate progress update "
            "will keep going through the retried 'report_progress', so a "
            "degraded backend can stall a handler for max_retries x "
            "lifecycle_timeout_s plus backoff on one update. Add a one-shot "
            "(no-retry) 'report_progress_once' with the same signature.",
            type(client).__qualname__,
        )
    return client.report_progress


@dataclass
class _SharedState:
    """Mutable state the heartbeat reads and handlers write."""

    stage: str = "starting"
    current: int = 0
    total: int = 0
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)


class ProgressReporter:
    """Handlers update progress via ``await progress.update(...)``.

    A separate background task reads the current state and emits
    heartbeats every ``heartbeat_interval_s``. The shared ``cancelled``
    event is set when the backend reports cancel; handlers call
    ``ProgressReporter.raise_if_cancelled()`` between blocking ops.
    """

    def __init__(
        self,
        client: "BackendClient",
        task_id: int,
        *,
        heartbeat_interval_s: float = 10.0,
        heartbeat_warn_threshold: int = 3,
    ):
        self._client = client
        self._task_id = task_id
        self._interval = heartbeat_interval_s
        self._warn_threshold = max(1, heartbeat_warn_threshold)
        self._state = _SharedState()
        self._task: Optional[asyncio.Task] = None
        # Consecutive heartbeat failures (reset to 0 on a successful tick).
        # Used to escalate the per-tick failure log from DEBUG to WARNING
        # once a sustained backend outage becomes unlikely to be transient.
        self._heartbeat_failures = 0
        # Optional external cancel event (from a CancelGuard). When linked,
        # is_cancelled / raise_if_cancelled also observe this event so a
        # handler polling ctx.progress.is_cancelled learns of a cancel at
        # the guard's poll latency (cancel_poll_interval_s, default 2s)
        # instead of the heartbeat's (heartbeat_interval_s, default 10s).
        self._external_cancelled: Optional[asyncio.Event] = None

    async def update(self, stage: str, current: int = 0, total: int = 0) -> None:
        """Handler progress update. Updates shared state; the heartbeat
        loop picks up the new values on its next tick. Also emits an
        immediate progress call so stage transitions land quickly.

        The immediate call is one-shot (``report_progress_once``): it runs on
        the handler's critical path, and the retried ``report_progress`` could
        hold it for ``max_retries`` × ``lifecycle_timeout_s`` plus backoff
        (~75s with SDK defaults) against a degraded backend — stalling the
        work the update was describing. Losing one immediate report costs
        stage-transition latency only: the new state is already in
        ``self._state``, and the background heartbeat re-sends it on its next
        tick through the retried call, which is what keeps ``updated_at``
        fresh across a backend blip.
        """
        self._state.stage = stage
        self._state.current = current
        self._state.total = total
        try:
            resp = await _one_shot_report(self._client)(
                self._task_id, stage=stage, current=current, total=total,
                kill_handle=_REMOTE_KILL_HANDLE,
            )
            if resp.get("cancelled"):
                self._state.cancelled.set()
        except Exception as e:  # noqa: BLE001
            log.warning(
                "progress update failed for task %s: %s",
                self._task_id, e,
            )

    async def start_heartbeat(self) -> None:
        """Start the background heartbeat loop. Call once per task."""
        if self._task is not None:
            raise RuntimeError("heartbeat already running")
        self._task = asyncio.create_task(
            self._heartbeat_loop(),
            name=f"heartbeat-task-{self._task_id}",
        )

    async def stop(self) -> None:
        """Stop the heartbeat loop cleanly."""
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        self._task = None

    def link_cancelled(self, event: Optional[asyncio.Event]) -> None:
        """Link an external cancel event (typically from a CancelGuard).

        After linking, :attr:`is_cancelled` and
        :meth:`raise_if_cancelled` also observe ``event`` in addition to
        the heartbeat's own ``_state.cancelled``. The CancelGuard polls
        ``/tasks/{id}/cancel-status`` every ``cancel_poll_interval_s``
        (default 2s) — faster than the heartbeat's
        ``heartbeat_interval_s`` (default 10s) — so handlers that poll
        ``ctx.progress.is_cancelled`` learn of a cancel at the guard's
        latency instead of waiting for the next heartbeat tick.

        Passing ``None`` unlinks a previously-linked event. Safe to call
        before or after :meth:`start_heartbeat`.
        """
        self._external_cancelled = event

    @property
    def is_cancelled(self) -> bool:
        if self._state.cancelled.is_set():
            return True
        return (
            self._external_cancelled is not None
            and self._external_cancelled.is_set()
        )

    def raise_if_cancelled(self) -> None:
        """Handlers call this between blocking ops to bail out on cancel."""
        if self.is_cancelled:
            from .errors import TaskCancelled
            raise TaskCancelled(f"task {self._task_id} cancelled by user")

    async def _heartbeat_loop(self) -> None:
        """Background heartbeat — best-effort, tolerates transient errors.

        A single failed tick (the BackendClient has already exhausted its
        own retries by this point) is logged at DEBUG: a transient blip
        during a long-running task is noise an operator doesn't need.
        But a *sustained* outage — N consecutive failures — means the
        backend is unreachable and the task's ``updated_at`` is going
        stale, which the sweeper will soon read as abandonment and
        reclaim. That escalates to WARNING so the outage is visible in
        production logs at the default level instead of masked at DEBUG.
        """
        while True:
            try:
                resp = await self._client.report_progress(
                    self._task_id,
                    stage=self._state.stage,
                    current=self._state.current,
                    total=self._state.total,
                    kill_handle=_REMOTE_KILL_HANDLE,
                )
                if resp.get("cancelled"):
                    self._state.cancelled.set()
                self._heartbeat_failures = 0
            except Exception as e:  # noqa: BLE001
                self._heartbeat_failures += 1
                if self._heartbeat_failures >= self._warn_threshold:
                    log.warning(
                        "heartbeat failed for task %s (%d consecutive "
                        "failures): %s",
                        self._task_id, self._heartbeat_failures, e,
                    )
                else:
                    log.debug(
                        "heartbeat failed for task %s: %s",
                        self._task_id, e,
                    )
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                return
