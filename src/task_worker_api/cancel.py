"""CancelGuard — background cancel-status poller with a termination hook.

Three canonical usage patterns:

1. Pure async handler — polls ``ctx.progress.is_cancelled`` between steps
   and returns (or raises ``TaskCancelled``) itself. Nothing interrupts a
   handler that doesn't: asyncio has no way to raise into another coroutine,
   and the guard deliberately does not cancel the handler's task, because
   cancelling an ``await`` does not stop the work behind it (pattern 3's
   thread would keep running, detached, holding the GPU). A handler that
   ignores the signal therefore runs to completion, and the cancel is
   honoured at the ``prepare_inputs``/``upload_outputs`` boundaries and on
   leaving the guarded block.
2. Subprocess handler (Blender, colmap) — ``on_cancel`` calls ``proc.terminate()``;
   the handler's ``await proc.communicate()`` unblocks; its
   ``ctx.progress.raise_if_cancelled()`` then raises.
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

#: Set once a client without ``poll_cancel_status`` has been warned about, so
#: a legacy client logs one WARNING per process rather than one per poll tick
#: of every task the worker runs.
_warned_legacy_cancel_client = False

#: Consecutive failed cancel polls before the per-tick DEBUG line escalates to
#: WARNING. A single blip during a long task is noise; a *sustained* failure
#: (rotated API key, 404 after the sweeper reclaimed the task, drifted base
#: URL) means the guard is blind for the rest of the task — the ``cancelled``
#: event never sets, ``on_cancel`` never terminates the subprocess, and the
#: file-transfer aborts never fire. Mirrors ``ProgressReporter``'s heartbeat
#: escalation; not a ``Worker`` knob because there is nothing to tune per
#: deployment.
_POLL_WARN_THRESHOLD = 3


def _cancel_status_poller(client: "BackendClient"):
    """``client.poll_cancel_status`` if it has one, else ``get_cancel_status``.

    ``poll_cancel_status`` (a single-shot GET with no client-level retries)
    was added after worker repos and test doubles had already grown their own
    clients against the older protocol (``Worker(client=...)`` takes any
    duck-typed client). Calling it unconditionally would raise
    ``AttributeError`` on the guard's very first tick of every such client;
    the poll loop swallows exceptions at DEBUG, so the failure mode is silent
    and total: the ``cancelled`` event never sets, ``on_cancel`` never fires
    (a subprocess handler like Blender/colmap is never terminated), and the
    event threaded into ``prepare_inputs``/``upload_outputs`` never aborts
    file transfers — the cancel-abort guarantees from the one-shot poll all
    quietly vanish. Falling back to ``get_cancel_status`` keeps those clients
    functional; they just keep the old worst case, where a degraded backend
    can delay cancel detection by the client's retry chain.
    """
    poll = getattr(client, "poll_cancel_status", None)
    if poll is not None:
        return poll
    global _warned_legacy_cancel_client
    if not _warned_legacy_cancel_client:
        _warned_legacy_cancel_client = True
        log.warning(
            "%s has no 'poll_cancel_status'; cancel polling falls back to "
            "the retried 'get_cancel_status', so a degraded backend can "
            "delay cancel detection by max_retries x cancel_timeout_s plus "
            "backoff (~50s with SDK defaults). Add a one-shot (no-retry) "
            "'poll_cancel_status' with the same signature.",
            type(client).__qualname__,
        )
    return client.get_cancel_status


@asynccontextmanager
async def CancelGuard(
    client: "BackendClient",
    task_id: int,
    *,
    poll_interval_s: float = 2.0,
    on_cancel: Optional[Callable[[], None]] = None,
):
    """Polls ``GET /tasks/{id}/cancel-status`` in the background.

    Uses ``BackendClient.poll_cancel_status`` — a single-shot GET with no
    client-level retries — so a degraded backend can only blind the guard
    for one ``cancel_timeout_s`` window (default 5s) before the guard
    retries on its own ``poll_interval_s`` schedule. Previously the guard
    used ``get_cancel_status``, which goes through ``_retry`` (4 attempts
    with exponential backoff); a slow backend could stall the guard for
    ~50s while the worker kept computing on a cancelled task.

    A duck-typed client that predates ``poll_cancel_status`` (``Worker(
    client=...)`` accepts any client) keeps working through the retried
    ``get_cancel_status``, with a one-time WARNING naming the method to
    add — cancel detection degrades to that call's worst case but is never
    silently dead.

    When cancelled:
      - Calls ``on_cancel()`` synchronously. This runs on the guard's
        task, so a ``subprocess.terminate()`` or ``threading.Event.set()``
        lands immediately.
      - Sets the yielded ``cancelled`` event. The guard does *not*
        interrupt the guarded block — see pattern 1 above — so the block
        must watch the event itself: ``prepare_inputs``/``upload_outputs``
        abort on it, and the handler stops on its own terms
        (``ctx.progress.is_cancelled``, or the ``on_cancel`` hook).
      - Raises ``TaskCancelled`` on exit of the guarded block if nothing
        inside it already did, so a cancel is never reported as a success.

    Timing: cancel visibility is bounded by ``poll_interval_s`` (default 2s)
    plus ``cancel_timeout_s`` (default 5s) on a degraded backend. Long C
    extension calls that don't yield to the event loop will see the cancel
    only after they return.
    """
    cancelled = asyncio.Event()
    poll_status = _cancel_status_poller(client)

    async def _poll():
        failures = 0
        # Escalate at 3 consecutive failures, then at each doubling (3, 6,
        # 12, ...) — a 3-hour task polling every 2s would otherwise emit a
        # WARNING every tick once the backend goes bad.
        warn_at = _POLL_WARN_THRESHOLD
        while not cancelled.is_set():
            try:
                resp = await poll_status(task_id)
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
                # Reset only once the response actually parsed: a backend
                # stuck returning a malformed 200 (``[]``, a bare string)
                # blinds the guard exactly like a connection error, so it
                # must escalate rather than reset the streak every tick.
                failures = 0
                warn_at = _POLL_WARN_THRESHOLD
            except Exception as e:  # noqa: BLE001
                failures += 1
                if failures >= warn_at:
                    warn_at = failures * 2
                    log.warning(
                        "cancel poll failed for task %s (%d consecutive "
                        "failures) — a cancel will go undetected until "
                        "polling recovers: %s",
                        task_id, failures, e,
                    )
                else:
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
