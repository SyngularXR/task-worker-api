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
    ):
        self._client = client
        self._task_id = task_id
        self._interval = heartbeat_interval_s
        self._state = _SharedState()
        self._task: Optional[asyncio.Task] = None

    async def update(self, stage: str, current: int = 0, total: int = 0) -> None:
        """Handler progress update. Updates shared state; the heartbeat
        loop picks up the new values on its next tick. Also emits an
        immediate progress call so stage transitions land quickly."""
        self._state.stage = stage
        self._state.current = current
        self._state.total = total
        try:
            resp = await self._client.report_progress(
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

    @property
    def is_cancelled(self) -> bool:
        return self._state.cancelled.is_set()

    def raise_if_cancelled(self) -> None:
        """Handlers call this between blocking ops to bail out on cancel."""
        if self._state.cancelled.is_set():
            from .errors import TaskCancelled
            raise TaskCancelled(f"task {self._task_id} cancelled by user")

    async def _heartbeat_loop(self) -> None:
        """Background heartbeat — best-effort, tolerates transient errors."""
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
            except Exception as e:  # noqa: BLE001
                log.debug(
                    "heartbeat failed for task %s: %s",
                    self._task_id, e,
                )
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                return
