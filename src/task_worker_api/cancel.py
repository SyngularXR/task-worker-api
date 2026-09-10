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
      - Raises ``TaskCancelled`` in the guarded block at the next
        ``await`` point. The poller cancels the task running the guarded
        block (the same move ``AttemptLease._watch`` makes via
        ``_owner.cancel()``) and converts the resulting ``CancelledError``
        back into ``TaskCancelled`` on the way out, so a pure-async
        handler awaiting a long operation is interrupted instead of
        running to completion on a task the user already cancelled, and
        ``Worker._execute_one`` still reports "cancelled by user" rather
        than the shutdown reason. A block that swallows the
        ``CancelledError`` still gets ``TaskCancelled`` on exit.

    Timing: cancel visibility is bounded by ``poll_interval_s`` (default 2s)
    plus ``cancel_timeout_s`` (default 5s) on a degraded backend. Long C
    extension calls that don't yield to the event loop will see the cancel
    only after they return.
    """
    cancelled = asyncio.Event()
    poll_status = _cancel_status_poller(client)
    # The task running the guarded block: cancelling it is what makes the
    # "raises at the next await point" guarantee real. None outside a task
    # (the guard then degrades to raising on exit, as it always did).
    owner = asyncio.current_task()
    # True once ``_poll`` has requested ``owner.cancel()`` — the only
    # CancelledError this guard may convert into TaskCancelled. A cancel from
    # anywhere else (worker shutdown) must keep propagating as-is.
    interrupted = False

    async def _poll():
        nonlocal interrupted
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
                    # After ``on_cancel`` so a subprocess terminate() or
                    # threading.Event.set() has already landed. Same loop as
                    # the owner, so a plain ``.cancel()`` — no
                    # call_soon_threadsafe, unlike AttemptLease._watch.
                    if owner is not None:
                        interrupted = True
                        owner.cancel()
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
        try:
            yield cancelled
        except asyncio.CancelledError:
            if not interrupted:
                raise  # someone else's cancel (shutdown) — stays a cancel
            raise TaskCancelled(
                f"task {task_id} cancelled by user"
            ) from None
        if cancelled.is_set():
            # The block finished before our cancel could be delivered: it
            # ended without another await, or swallowed the CancelledError.
            raise TaskCancelled(f"task {task_id} cancelled by user")
    finally:
        # Balance our own ``cancel()`` on every exit path (converted,
        # swallowed by the block, or overtaken by another exception) and
        # before the await below: an undelivered request would otherwise fire
        # at some later await — the worker's terminal report — and an
        # enclosing TaskGroup/asyncio.timeout must see the cancellation count
        # it started with. ``uncancel`` is 3.11+; on 3.10 (requires-python
        # still allows it) the pending request is instead absorbed by the
        # drain below, which already swallows CancelledError.
        if interrupted and hasattr(owner, "uncancel"):
            owner.uncancel()
        poll_task.cancel()
        try:
            await poll_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
