"""CancelGuard — background cancel-status poller with a termination hook.

Three canonical usage patterns:

1. Pure async handler — TaskCancelled raises at the next ``await``.
2. Subprocess handler (Blender, colmap) — ``on_cancel`` calls ``proc.terminate()``;
   the handler's ``await proc.communicate()`` unblocks; the guard raises on
   the next poll tick.
3. Threadpool handler (Neural-Canvas GPU work) — ``on_cancel`` sets a
   ``threading.Event``; the thread checks the event between iterations
   and raises ``TaskCancelled`` from within its synchronous loop.

See the design spec at
docs/specs/2026-04-22-unified-task-queue-api-contract-design.md §Cancel patterns.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Callable, Optional, TYPE_CHECKING

from .errors import TaskCancelled

if TYPE_CHECKING:  # pragma: no cover
    from .client import BackendClient

log = logging.getLogger(__name__)


@asynccontextmanager
async def CancelGuard(
    client: "BackendClient",
    task_id: int,
    *,
    poll_interval_s: float = 2.0,
    on_cancel: Optional[Callable[[], None]] = None,
):
    """Polls ``GET /tasks/{id}/cancel-status`` in the background.

    When cancelled:
      - Calls ``on_cancel()`` synchronously. This runs on the guard's
        task, so a ``subprocess.terminate()`` or ``threading.Event.set()``
        lands immediately.
      - Raises ``TaskCancelled`` in the guarded block at the next
        ``await`` point.

    Timing: cancel visibility is bounded by ``poll_interval_s`` (default 2s).
    Long C extension calls that don't yield to the event loop will see
    the cancel only after they return.
    """
    cancelled = asyncio.Event()

    async def _poll():
        while not cancelled.is_set():
            try:
                resp = await client.get_cancel_status(task_id)
                if resp.get("cancelled"):
                    cancelled.set()
                    if on_cancel is not None:
                        try:
                            on_cancel()
                        except Exception as e:  # noqa: BLE001
                            log.warning(
                                "on_cancel hook for task %s raised: %s",
                                task_id, e,
                            )
                    return
            except Exception as e:  # noqa: BLE001
                log.debug(
                    "cancel poll failed for task %s: %s",
                    task_id, e,
                )
            try:
                await asyncio.sleep(poll_interval_s)
            except asyncio.CancelledError:
                return

    poll_task = asyncio.create_task(
        _poll(), name=f"cancel-guard-{task_id}",
    )

    try:
        yield cancelled
        if cancelled.is_set():
            raise TaskCancelled(f"task {task_id} cancelled by user")
    finally:
        poll_task.cancel()
        try:
            await poll_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
