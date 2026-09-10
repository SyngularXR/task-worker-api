"""Async HTTP client for the SynPusher worker protocol.

Thin wrapper over ``httpx.AsyncClient`` with retry-on-transient-error.
The wire format (paths, methods, bodies) is intentionally identical to
the pre-SDK shape — this client consolidates three divergent copies
(Blender-CLI, Neural-Canvas, colmap-splat) into one reviewed place.
"""
from __future__ import annotations

import logging
import math
import random
import time
from contextlib import aclosing
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

import httpx

from .context import ClaimedTask
from .errors import ProtocolError, TaskCancelled

if TYPE_CHECKING:
    import asyncio

    from .payload_log import PayloadLogger

log = logging.getLogger(__name__)

# Transient error classes that get retried with exponential backoff.
_RETRYABLE_EXCEPTIONS = (httpx.TransportError, httpx.TimeoutException)

# HTTP status codes that represent a *transiently* unavailable upstream and
# therefore warrant a retry. The backend sits behind nginx; a 502/503/504 on
# a worker request almost always means the Flask app restarted, the gateway
# timed out, or the upstream connection was refused — a blip that clears in
# seconds. Retrying these (instead of failing the task outright) lets a worker
# ride through a backend redeploy or a momentary load spike.
#
# 429 (Too Many Requests) is included because the shared backend serves 3+
# workers (Neural-Canvas, Blender-CLI, colmap-splat); under burst load it can
# rate-limit a lifecycle call (complete/fail/progress). Dropping the terminal
# status on a 429 leaves the task stuck in_progress until the sweeper reclaims
# it — retrying with backoff lets the worker self-heal instead.
#
# 408 (Request Timeout) is included because it is a *timeout*, not a client
# error: nginx returns it when a request's headers or body did not arrive
# inside ``client_header_timeout``/``client_body_timeout``, i.e. exactly the
# slow-link blip that already gets retried when it surfaces as an
# ``httpx.TimeoutException`` instead of a status. A worker streaming a
# GB-scale output (colmap-splat PLY, Neural-Canvas splat) over a congested
# link is the case that hits it, and failing the upload there fails a task
# whose work is already done. RFC 9110 §15.5.9 sanctions the retry directly:
# the client "MAY repeat the request without modifications at any later
# time". Every route this client calls is an idempotent guarded transition,
# so re-sending is safe.
#
# 500 is intentionally excluded: a 500 is the application's own error
# response, which usually signals a logic bug or a bad payload, not a
# transient outage — retrying it just burns budget and re-logs the same error.
# The other 4xx codes are excluded for the same reason (client error, retrying
# won't help) — 408 is the exception because the server is reporting a
# transport-level timeout rather than a defect in the request.
#
# Exception: terminal reports (``complete``/``fail``) opt in to retrying 500
# via ``_retry``'s ``extra_transient`` parameter. A 500 there can also mean
# the backend's own dependency died mid-write (e.g. Postgres I/O error), and
# dropping the report orphans a fully computed outcome — the task stays
# RUNNING until the sweeper reclaims it. Both terminal routes are idempotent
# guarded transitions, so re-PUTting is safe.
_TRANSIENT_STATUS_CODES = frozenset({408, 429, 502, 503, 504})

# Extra transient set + attempt floor for terminal reports (see above).
_TERMINAL_EXTRA_TRANSIENT = frozenset({500})
_TERMINAL_MIN_ATTEMPTS = 6

# Default ceiling for a single retry delay. Without a cap, backoff grows as
# ``retry_backoff_s * 2**n`` — unbounded. A worker configured with the
# (supported) ``max_retries=8`` and the default ``retry_backoff_s=2.0`` would
# wait 256s on the 7th retry, blocking its event loop for ~10 minutes on a
# single claim/complete call. The cap keeps individual delays sane while still
# allowing long total retry windows across many attempts. Consumers can raise
# it via ``retry_backoff_max_s`` if they genuinely want longer waits.
_DEFAULT_BACKOFF_MAX_S = 60.0

# ``Retry-After`` is server guidance, not exponential backoff, so the backoff
# ceiling must not shorten it. Still bound hostile or broken headers: six hours
# is long enough for normal maintenance and rate-limit windows without
# accepting an effectively infinite sleep.
_MAX_RETRY_AFTER_S = 6 * 60 * 60

# Jitter spread: each delay is multiplied by a uniform random factor in
# ``[1 - JITTER, 1 + JITTER]``. ±25% is the AWS-recommended "full jitter"
# band — enough to decorrelate the fleet (Neural-Canvas, Blender-CLI,
# colmap-splat all poll the same backend) without making delays unpredictable
# enough to mask scheduling bugs in tests.
_JITTER_SPREAD = 0.25

# Default timeout for file transfer operations (download_file / upload_file).
# GB-scale outputs (colmap-splat PLY files, Neural-Canvas splats) can take
# minutes to stream over a typical backend link; the 30s general request
# timeout that governs claim/heartbeat/complete is far too tight for them and
# produces spurious WriteTimeout/ReadTimeout failures on tasks that would
# otherwise succeed. This is the default for the separate ``file_timeout_s``
# parameter; consumers can override it per Worker/BackendClient.
_DEFAULT_FILE_TIMEOUT_S = 300.0

# Bytes buffered per ``download_file`` disk write. ``aiter_bytes()`` yields
# whatever the transport hands over — typically ~64 KB — and each write is a
# blocking filesystem call, so writing every wire chunk straight through would
# mean tens of thousands of thread dispatches per GB. Chunks are accumulated
# to this size and the buffer is written off the event-loop thread. Same
# trade-off as ``files._COPY_CHUNK_BYTES``: large enough that a multi-GB
# transfer is not dispatch-bound, small enough that memory stays bounded.
# This is a *write* granularity only — ``cancelled`` is still checked at every
# chunk the transport delivers, so it does not delay a cancel.
_DOWNLOAD_CHUNK_BYTES = 1024 * 1024

# Bytes read per ``upload_file`` disk read, mirroring _DOWNLOAD_CHUNK_BYTES.
# httpx's own multipart encoder reads the file object in 64 KB chunks
# (``FileField.CHUNK_SIZE``) from inside ``MultipartStream.__aiter__``, i.e.
# synchronously on the event-loop thread; reading 1 MB at a time off-loop
# keeps the thread dispatches proportional to the file size instead.
_UPLOAD_CHUNK_BYTES = 1024 * 1024

# Cap on the serialized ``fail`` body. A handler that raises with megabytes of
# subprocess stderr (colmap-splat, Blender-CLI) or a RecursionError traceback
# produces a body nginx rejects with 413 or the app with 400 — neither is in
# the transient set, so the terminal report is lost outright and the task
# orphans in_progress until the sweeper reclaims it: the failure reason is
# exactly what makes the report undeliverable. 16 KB is generous against any
# real message and far under nginx's 1 MB default body limit.
_MAX_FAIL_ERROR_BYTES = 16 * 1024


# Any absolute URL works: building a request encodes the body and transmits
# nothing. Same probe trick as ``worker._result_encode_exc``.
_ENCODE_PROBE_URL = "http://encode-check.invalid/"


def _encodable(error: str) -> str:
    """``error`` with anything httpx cannot encode replaced by its escape.

    A traceback quoting subprocess output decoded with ``surrogateescape``
    carries lone surrogates, and httpx 0.28 encodes the body as strict UTF-8 —
    so an unsanitized one raises ``UnicodeEncodeError`` while httpx *builds*
    the request, before any transport sees it, inside the very call meant to
    keep the report deliverable. ``backslashreplace`` is the identity for
    anything already encodable, so no real failure reason is rewritten, and it
    renders what it does replace legibly (``\\udcff``) instead of dropping it.
    """
    return error.encode("utf-8", "backslashreplace").decode("utf-8")


def _fail_body_bytes(error: str) -> int:
    """Exact size of the ``fail`` body httpx will put on the wire.

    Asks httpx itself, for the same reason as ``worker._result_encode_exc``:
    the encoder's flags moved across the declared ``httpx>=0.23`` range — 0.28
    switched ``encode_json`` to ``ensure_ascii=False`` with compact separators
    — so a re-implementation here drifts from the installed encoder, and one
    that over-measures truncates an error that would have fit.

    Measures the whole document rather than the raw string because escaping
    still expands a code point (to 6 bytes for a control character), so a cap
    on the string alone is no bound on the wire. ``error`` must already be
    :func:`_encodable`.
    """
    return len(
        httpx.Request("PUT", _ENCODE_PROBE_URL, json={"error": error}).content
    )


def _cap_fail_error(error: str) -> str:
    """Bound ``error`` so the terminal report stays deliverable.

    Keeps a head and a tail rather than just a head: the head carries the entry
    point and the tail carries the exception type and message, which is the
    part that names the failure. Slices are taken by code point (never mid
    character) and the result is re-measured, because the per-character cost of
    escaping is not known in advance.
    """
    error = _encodable(error)
    if _fail_body_bytes(error) <= _MAX_FAIL_ERROR_BYTES:
        return error
    total = len(error.encode("utf-8"))
    # A quarter of the cap per side leaves room for escaping, the marker and
    # the JSON wrapper; halve until it actually fits.
    keep = min(_MAX_FAIL_ERROR_BYTES // 4, len(error) // 2)
    while keep:
        head, tail = error[:keep], error[-keep:]
        dropped = total - len(head.encode("utf-8")) - len(tail.encode("utf-8"))
        capped = f"{head}\n...[{dropped} bytes truncated]...\n{tail}"
        if _fail_body_bytes(capped) <= _MAX_FAIL_ERROR_BYTES:
            return capped
        keep //= 2
    return f"...[{total} bytes truncated]..."


def _is_transient_status(
    exc: httpx.HTTPStatusError,
    extra: frozenset = frozenset(),
) -> bool:
    """True iff a status error's code is a transiently-retryable code."""
    return exc.response.status_code in _TRANSIENT_STATUS_CODES or (
        exc.response.status_code in extra
    )


def _retry_after_delay(response: httpx.Response, *, maximum_seconds: Optional[int] = _MAX_RETRY_AFTER_S) -> Optional[float]:
    """Parse a response's ``Retry-After`` header into a delay in seconds.

    RFC 9110 allows two forms and both are accepted: delta-seconds
    (``Retry-After: 30``) and an HTTP-date (``Retry-After: Wed, 21 Oct 2026
    07:28:00 GMT``, converted to a delay relative to now).

    Returns ``None`` only when the header carries no usable guidance — absent
    or malformed. The caller then falls back to its own exponential schedule.
    By default valid delays are capped at ``_MAX_RETRY_AFTER_S``; admission v2
    passes maximum_seconds=None so server guidance is never shortened. This is deliberately
    separate from ``retry_backoff_max_s``: a 60-second exponential-backoff cap
    must not turn ``Retry-After: 3600`` into six requests inside a one-hour
    rate-limit window.

    ``0.0`` is a real answer, not "no guidance": ``Retry-After: 0`` and an
    HTTP-date already in the past both mean *retry immediately* (RFC 9110
    §10.2.3), and the attempt budget still bounds how many requests that can
    produce. A negative *delta-seconds* is a different case — delta-seconds is
    defined as a non-negative integer, so ``Retry-After: -5`` is malformed
    input rather than guidance, and falls back to the schedule.

    The header is remote input. Oversized delta-seconds are capped before the
    conversion to ``float``; malformed dates degrade to our schedule rather
    than replacing the expected ``HTTPStatusError`` with a parser exception.
    """
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    value = raw.strip()
    if value.isascii() and value.isdecimal():
        # Compare decimal text before conversion. Besides avoiding float
        # overflow, this avoids Python's length limit and conversion cost for
        # an attacker-controlled string containing thousands of digits.
        value = value.lstrip("0") or "0"
        if maximum_seconds is not None:
            ceiling = str(maximum_seconds)
            if len(value) > len(ceiling) or (len(value) == len(ceiling) and value > ceiling):
                return float(maximum_seconds)
        delay = float(value)
        if not math.isfinite(delay):
            raise ProtocolError("Retry-After exceeds a representable wait; refusing an early retry")
        return delay
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if when is None:  # pragma: no cover — pre-3.10 returned None, not raise
        return None
    if when.tzinfo is None:
        # An HTTP-date without a zone is still GMT per RFC 9110 (obs-date
        # forms parse tz-naive); interpreting it as local time would skew
        # the delay by the host's UTC offset.
        when = when.replace(tzinfo=timezone.utc)
    # A date already past means "retry now", not "no guidance".
    delay = max((when - datetime.now(timezone.utc)).total_seconds(), 0.0)
    return min(delay, maximum_seconds) if maximum_seconds is not None else delay


def _backoff_delay(
    attempt: int,
    base_s: float,
    max_s: Optional[float],
    jitter: bool,
    *,
    rng: Optional[random.Random] = None,
) -> float:
    """Compute one retry delay: capped exponential backoff with optional jitter.

    The base schedule is ``base_s * 2**attempt`` (deterministic, matching the
    pre-existing contract). Two guards make it production-safe across a fleet:

    - **Cap**: the delay is clamped to ``max_s`` so a high ``max_retries``
      can't produce a single multi-minute sleep.
    - **Jitter**: when enabled, the delay is multiplied by a uniform random
      factor in ``[1 - _JITTER_SPREAD, 1 + _JITTER_SPREAD]``. The three fleet
      workers share one backend; without jitter they'd all retry on the
      identical deterministic schedule and re-overload it the instant it
      recovers (thundering herd). Jitter decorrelates them.

    The two compose one way only: the jitter **band** is clipped to ``max_s``,
    so the returned delay never exceeds the cap. Jittering symmetrically and
    handing the result straight back would return up to ``max_s * 1.25`` —
    overshooting the ceiling that exists precisely to bound how long one call
    may block the worker's single-task polling loop, and doing it on every
    delay from ``0.8 * max_s`` upward, which is most of them once the
    exponential has climbed. Clamping the drawn *value* instead would honour
    the ceiling but pile every over-cap draw onto ``max_s`` exactly, re-syncing
    the workers that jittered upward — the herd this is here to break up.
    Clipping the band keeps the delay a continuous spread (at the cap:
    ``[0.75 * max_s, max_s]``): still decorrelated, never over budget. Same
    shape, and the same reasoning, as ``Worker._claim_wait_s``.

    ``rng`` is injectable so tests can assert on exact delays deterministically.
    """
    delay = base_s * (2**attempt)
    if max_s is not None and delay > max_s:
        delay = max_s
    if jitter and delay > 0:
        r = rng if rng is not None else random
        spread = delay * _JITTER_SPREAD
        high = delay + spread
        if max_s is not None and high > max_s:
            high = max_s
        delay = r.uniform(delay - spread, high)
    return delay


def _progress_body(
    stage: str, current: int, total: int, kill_handle: Optional[dict],
) -> dict[str, Any]:
    """Wire body for PUT /tasks/{id}/progress.

    Shared by the retried :meth:`BackendClient.report_progress` and the
    one-shot :meth:`BackendClient.report_progress_once` so the two can never
    drift into sending different shapes for the same endpoint.
    """
    body: dict[str, Any] = {"stage": stage, "current": current, "total": total}
    if kill_handle is not None:
        body["kill_handle"] = kill_handle
    return body


def _per_request_timeout(seconds: Optional[float]):
    """``httpx.Timeout(seconds)``, or the inherit-the-client-default sentinel.

    The sentinel must be ``httpx.USE_CLIENT_DEFAULT`` and **not** ``None``.
    httpx resolves a per-request ``timeout=`` in ``build_request``, before the
    transport ever runs: an explicit ``None`` there becomes ``Timeout(None)``
    — every facet (connect/read/write/pool) disabled, i.e. *no timeout at
    all* — and only the sentinel falls through to the client's own timeout.

    Passing ``None`` therefore inverted each of the documented opt-outs
    (``file_timeout_s=None``, ``cancel_timeout_s=None``,
    ``lifecycle_timeout_s=None``, all described as "falls back to the client's
    own timeout") into an *unbounded* request against an unresponsive backend:

    - a cancel poll that never returns leaves the ``CancelGuard`` blind for
      the rest of the task, so a user cancel is never acted on;
    - a heartbeat or terminal ``complete``/``fail`` that never returns wedges
      the worker's single-task polling loop — no claims, no cancel polls, no
      shutdown response — while the sweeper reclaims the task as abandoned;
    - a ``download_file`` that never returns strands a partial file at
      ``dest``, because the cleanup only runs on the way out.

    None of it is reachable via retry or backoff: the hang is inside one
    request, so ``_retry`` never sees a failure to retry.
    """
    return httpx.USE_CLIENT_DEFAULT if seconds is None else httpx.Timeout(seconds)


def _validate_timeout_s(
    name: str, value: Optional[float], *, allow_none: bool,
) -> None:
    """Validate one HTTP deadline knob, or raise ``ValueError``.

    Every one of these is handed straight to ``httpx.Timeout``, which accepts
    any float and turns each degenerate value into a different silent failure
    — silent at construction, and only visible once the deadline it was meant
    to impose is the thing standing between the worker and a wedged backend.
    Consumers build them from the environment, so a typo'd value is a live
    path, not a hypothetical:

    * **NaN** becomes an anyio deadline of NaN, and every comparison against
      NaN is False — so the deadline never fires and the request hangs
      unbounded. That is exactly the hang ``_per_request_timeout``'s docstring
      documents for a literal ``None``, reached through a different door: a
      cancel poll that never returns leaves ``CancelGuard`` blind for the rest
      of the task, a lifecycle write that never returns wedges the polling
      loop while the sweeper reclaims the task.
    * **inf** disables the deadline outright — the same unbounded request,
      spelled as a number.
    * **Negative** puts the deadline in the past, so *every* request fails
      instantly: cancel polls always fail (the guard is blind again, this time
      loudly), and lifecycle writes always fail — the terminal complete/fail
      report is lost and the task is orphaned ``in_progress``.
    * **0** is that same instant failure spelled as zero; unlike
      ``retry_backoff_s=0`` there is nothing a caller can mean by it.

    ``None`` is legal only where it is documented as the fall-back-to-the-
    client's-own-timeout opt-out (file/cancel/lifecycle); ``timeout_s`` is the
    client default itself and has nothing to fall back to.
    """
    if value is None and allow_none:
        return
    if value is None or not (math.isfinite(value) and value > 0):
        hint = "; pass None to inherit the client's own timeout." if allow_none else ""
        raise ValueError(
            f"{name} must be a finite number > 0 (got {value!r}){hint}"
        )


async def _drain_ignoring_cancel(task: "asyncio.Future") -> None:
    """Wait for ``task`` to actually finish, however often *we* are cancelled.

    ``asyncio.shield`` only defers the *first* cancellation: once it has
    raised, a bare ``await task`` is cancellable again, so a second
    ``cancel()`` — a worker shutdown landing on top of a user cancel, or
    :func:`_cancel_and_drain` on top of either — abandons the thread
    mid-flight. Re-shielding until the task is genuinely done rides out every
    extra cancel.

    The extra ``CancelledError`` itself is dropped, so a caller that must
    still report cancellation raises on its own afterwards
    (:func:`_to_thread_complete` re-raises; :func:`_await_unless_cancelled`
    raises :class:`TaskCancelled`). Deferring the cancel by a drain is the
    point: an abandoned drain is what leaves a thread writing into a handle
    the caller has already unwound past.

    Whatever the task raises is discarded here, matching
    :func:`_cancel_and_drain` — we are already unwinding, and a failing
    ``close()`` must not mask the reason why.
    """
    import asyncio

    while not task.done():
        try:
            await asyncio.shield(task)
        except BaseException:
            pass


async def _cancel_and_drain(task: "asyncio.Future") -> None:
    """Cancel ``task`` and wait for it to actually stop.

    ``cancel()`` only *requests* cancellation — it schedules a
    ``CancelledError`` for the next time the loop resumes the task, so a
    caller that moves on straight after cancelling leaves it running. For an
    in-flight PUT that means the request can still be reading ``src`` after
    ``upload_file`` closed it, or tearing its connection down after the
    client shut down: exactly the detached background work the cancel was
    supposed to stop. Whatever the task raises on the way out (the
    ``CancelledError`` we asked for, or a transport error from the severed
    connection) is discarded — it must not mask the reason we are unwinding.

    The wait itself rides out cancellation of *us* via
    :func:`_drain_ignoring_cancel`: a bare ``await task`` here would abandon
    the drain the moment a second cancel landed (worker shutdown on top of a
    user cancel), which is precisely the detached-and-still-reading state
    this function exists to prevent.
    """
    task.cancel()
    await _drain_ignoring_cancel(task)


def _discard_outcome(task: "asyncio.Future") -> None:
    """Retrieve an abandoned task's outcome so asyncio does not log it as an
    unretrieved exception. We have already reported why we stopped waiting."""
    if not task.cancelled():
        task.exception()


async def _cancel_and_drain_bounded(
    task: "asyncio.Future", timeout: float, message: str,
) -> None:
    """Cancel ``task``, wait at most ``timeout`` for it to stop, then give up.

    :func:`_cancel_and_drain` waits forever, which is right for our own
    transfer coroutines — they unwind promptly and the caller is about to
    close a file handle underneath them — but not for a worker author's
    handler. A handler that swallows ``CancelledError`` — a bare ``except``
    around the work loop, or cleanup that keeps awaiting — hangs the drain:
    the unbounded run the cancel race exists to prevent, moved into cleanup,
    and un-interruptible because :func:`_drain_ignoring_cancel` deliberately
    rides out cancellation of *us* too, so not even the caller's own timeout
    can break it.

    Past the timeout the task is abandoned. That leaves it running detached,
    which an aborted ``to_thread`` handler already does — the abort ends the
    await, never the work behind it — and is the lesser evil against never
    reporting the cancelled task at all.

    ``timeout`` only bites on cleanup that yields. Cleanup that blocks the
    event loop outright holds the loop this ``asyncio.wait`` timer runs on,
    so the timeout cannot fire until the block ends; see
    :func:`_await_unless_cancelled` for why nothing on the loop can bound
    that.
    """
    import asyncio

    task.cancel()
    try:
        done, _ = await asyncio.wait((task,), timeout=timeout)
        if not done:
            log.warning(
                "%s: handler did not unwind within %ss of the abort; "
                "reporting the cancel, handler left running detached",
                message, timeout,
            )
    finally:
        task.add_done_callback(_discard_outcome)


async def _to_thread_complete(func, /, *args, cancel_cleanup=None):
    """Do not let task cancellation race a blocking thread operation.

    On the cancel path the thread is drained — and ``cancel_cleanup`` run on
    whatever it produced — through :func:`_drain_ignoring_cancel`, so a
    repeated cancel cannot orphan the handle ``open`` was in the middle of
    returning. ``cancel_cleanup`` is skipped when the call failed, since
    there is then no result to clean up.
    """
    import asyncio

    task = asyncio.create_task(asyncio.to_thread(func, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await _drain_ignoring_cancel(task)
        if cancel_cleanup is not None and not task.cancelled() and task.exception() is None:
            await _drain_ignoring_cancel(
                asyncio.create_task(asyncio.to_thread(cancel_cleanup, task.result()))
            )
        raise


def _multipart_frame(field: str, filename: str) -> tuple[str, bytes, bytes]:
    """Return ``(content_type, prologue, epilogue)`` for a one-file body.

    The framing is generated by httpx itself from an empty placeholder file,
    so the boundary and the part headers — including httpx's own escaping of
    a non-ASCII filename and its ``mimetypes`` guess for the part's
    Content-Type — are byte-identical to what ``files={field: (filename, f)}``
    would have produced. Only the file *bytes* in the middle are ours to
    stream, which is the whole point: httpx renders that middle by calling
    ``file.read()`` synchronously on the event-loop thread.

    The body of the placeholder request is exactly ``prologue + epilogue``
    (the empty file contributes no bytes), and the epilogue is the RFC 2046
    closing delimiter, so slicing it off the end recovers the prologue.
    """
    probe = httpx.Request("PUT", "/", files={field: (filename, b"")})
    content_type = probe.headers["Content-Type"]
    boundary = content_type.partition("boundary=")[2]
    epilogue = f"\r\n--{boundary}--\r\n".encode("ascii")
    body = probe.read()
    if not body.endswith(epilogue):
        raise ProtocolError(f"unexpected httpx multipart framing: {body!r}")
    return content_type, body[: -len(epilogue)], epilogue


async def _multipart_file_body(
    src: Path, prologue: bytes, epilogue: bytes, size: int,
):
    """Stream ``src`` as a multipart body, reading it off the event loop.

    Opening, reading and closing all run through :func:`_to_thread_complete`,
    exactly as ``download_file`` does for its writes. A fresh generator per
    attempt re-opens ``src``, so a retry starts at byte 0.

    ``size`` is what the caller declared as Content-Length; a file that grew
    or shrank between the ``stat`` and the read would otherwise put a body of
    the wrong length on the wire, which the backend sees as a truncated (or
    hung) upload rather than an error. Raising instead fails the attempt
    loudly.
    """
    yield prologue
    sent = 0
    f = await _to_thread_complete(
        open, src, "rb", cancel_cleanup=lambda opened: opened.close(),
    )
    try:
        while chunk := await _to_thread_complete(f.read, _UPLOAD_CHUNK_BYTES):
            sent += len(chunk)
            yield chunk
    finally:
        await _to_thread_complete(f.close)
    if sent != size:
        raise ProtocolError(
            f"{src} changed size during upload: declared {size} bytes, read {sent}"
        )
    yield epilogue


async def _await_unless_cancelled(
    coro, cancelled: "asyncio.Event", message: str, *, grace_s: float = 0.0,
):
    """Await ``coro``, aborting it as soon as ``cancelled`` is set.

    Used around each file transfer's complete retry loop, so cancellation
    interrupts both an in-flight request and any retry backoff, and around
    the handler call in ``Worker._execute_one``, so a user cancel stops an
    in-flight handler instead of waiting for it to finish. The operation
    runs as a task and races the event: whichever finishes first wins, and if
    the event wins the operation is cancelled and :class:`TaskCancelled` is
    raised with ``message``.

    An operation that has already completed wins the tie, matching the
    last-chunk-wins behaviour of ``download_file`` and ``_copyfile_async``:
    the bytes are on the backend either way, so reporting a cancel that
    arrived after delivery would be a lie about what happened.

    ``grace_s`` (0 for file transfers, ``Worker.cancel_grace_s`` for the
    handler call) is how long the operation gets to notice the cancel and
    stop on its own terms before it is aborted. A handler that watches the
    signal itself — ``ctx.progress.is_cancelled``, or an ``on_cancel`` hook
    that terminates a subprocess or sets a ``threading.Event`` — must be
    allowed to finish its own unwind: aborting a ``to_thread`` await does
    not stop the thread behind it, it only detaches it, so the worker would
    claim its next task while the cancelled one still holds the GPU. Past
    the grace the operation is aborted anyway; the point of the race is that
    a handler which ignores the cancel cannot run unbounded.

    ``grace_s`` bounds the unwind too, via :func:`_cancel_and_drain_bounded`:
    the code being aborted is then the worker author's, and a handler that
    swallows the ``CancelledError`` would otherwise stall the drain forever,
    putting the reporting delay right back to unbounded. So a cancel is
    reported at most ``2 * grace_s`` after it lands — once to stop itself,
    once to unwind — and a handler that used neither is left detached.

    That bound holds only while the handler *yields to the event loop*, which
    every ``await``-based unwind does. It is not enforceable against cleanup
    that blocks the loop synchronously (``time.sleep``, a blocking
    ``thread.join()``, a GIL-holding C call inside ``except
    CancelledError:``): both graces are ``asyncio`` timeouts, and their timers
    only fire when the loop gets to run, so the very thing being bounded is
    what stops the bound from firing. Nothing scheduled on the loop can
    bound that — it equally stalls heartbeats and cancel polling — so it is
    the same Python limitation as a GIL-holding extension, not a property of
    this race. Blocking cleanup stays *correct* (the task is reported
    cancelled once the loop runs again, never as a success); it is only the
    timing that is unbounded.

    If the *caller* is cancelled while waiting (worker shutdown), the
    operation is cancelled too rather than left running detached with a file
    handle open.

    Every exit drains both children before returning or raising: cancellation
    is cooperative, so merely requesting it would let the PUT run on past the
    ``with open(src)`` block that ``upload_file`` is unwinding out of.
    """
    import asyncio

    async def abort(task):
        """Stop ``request``. Our own transfer coroutines are drained to
        completion; a handler only gets ``grace_s`` before it is abandoned."""
        if grace_s:
            await _cancel_and_drain_bounded(task, grace_s, message)
        else:
            await _cancel_and_drain(task)

    request = asyncio.ensure_future(coro)
    waiter = asyncio.ensure_future(cancelled.wait())
    try:
        await asyncio.wait(
            (request, waiter), return_when=asyncio.FIRST_COMPLETED,
        )
    except BaseException:
        # asyncio.wait does not cancel its futures when the awaiting task is
        # cancelled; without this the PUT would keep streaming after the
        # worker moved on.
        await abort(request)
        raise
    finally:
        await _cancel_and_drain(waiter)

    if grace_s and not request.done():
        try:
            # asyncio.wait leaves its futures alone on timeout — unlike
            # wait_for, which would cancel ``request`` and lose the drain.
            await asyncio.wait((request,), timeout=grace_s)
        except BaseException:
            await abort(request)
            raise

    if request.done():
        return request.result()

    # The operation is aborted on purpose; wait for it to unwind so the
    # connection is closed and the body has stopped reading src before
    # upload_file's `with open(src)` closes the handle underneath it.
    await abort(request)
    raise TaskCancelled(message)


class BackendClient:
    """Async HTTP client bound to one SynPusher backend URL + one worker key.

    Usage:
        async with BackendClient(url, api_key, worker_id="worker-1") as client:
            task = await client.claim_next(types, worker_id="...")
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        worker_id: Optional[str] = None,
        timeout_s: float = 30.0,
        file_timeout_s: Optional[float] = None,
        cancel_timeout_s: Optional[float] = 5.0,
        lifecycle_timeout_s: Optional[float] = 15.0,
        max_retries: int = 4,
        retry_backoff_s: float = 2.0,
        retry_backoff_max_s: Optional[float] = _DEFAULT_BACKOFF_MAX_S,
        retry_sleep_budget_s: Optional[float] = None,
        retry_jitter: bool = True,
        client: Optional[httpx.AsyncClient] = None,
        payload_logger: Optional["PayloadLogger"] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._worker_params = {"worker_id": worker_id} if worker_id else {}
        # max_retries is the total number of *attempts* (not retries-on-top-of-
        # one). A value < 1 means the retry loop in _retry never executes, so
        # last_exc stays None and the post-loop assert fires — an opaque
        # AssertionError that crashes the worker. Fail fast at construction
        # with a clear message instead.
        if max_retries < 1:
            raise ValueError(
                f"max_retries must be >= 1 (got {max_retries}); "
                "it is the total number of attempts, not retries on top of one."
            )
        self.max_retries = max_retries
        # Base of the exponential schedule (``retry_backoff_s * 2**n``) — the
        # one backoff knob that was unguarded, while every degenerate value
        # disables or breaks the very backoff it configures, silently at
        # construction and only visibly once a transient failure lands in
        # production:
        #
        # - **Negative** makes every delay negative. ``retry_backoff_max_s``
        #   only clamps from above and ``_backoff_delay`` skips jitter on a
        #   non-positive delay, so ``asyncio.sleep`` returns immediately on
        #   every attempt: the whole attempt budget is spent re-hammering a
        #   struggling backend with no spacing and no decorrelation — the
        #   retry storm the schedule exists to prevent, from the knob that
        #   configures it.
        # - **NaN** fails every comparison, so it passes the cap untouched and
        #   reaches ``asyncio.sleep``, which rejects it with ``ValueError:
        #   Invalid delay: NaN``. That is neither a retryable exception nor an
        #   ``HTTPStatusError``, so it escapes ``_retry`` mid-loop and replaces
        #   the transient error the caller was meant to see and handle.
        # - **inf** clamps to the cap when there is one, but
        #   ``retry_backoff_max_s=None`` is supported, and uncapped the jitter
        #   band is ``uniform(inf - inf, inf)`` → NaN → the ValueError above.
        #
        # ``0`` stays legal: it is how a caller (and this repo's own suite)
        # asks for retries with no inter-attempt sleep, degenerate only in the
        # way that was requested. Same finite-and-sane rule the poll loop's
        # knobs get in ``worker._positive_finite_s``, minus its zero rejection.
        if not (math.isfinite(retry_backoff_s) and retry_backoff_s >= 0):
            raise ValueError(
                f"retry_backoff_s must be a finite number >= 0 "
                f"(got {retry_backoff_s!r}); it is the base of the "
                "exponential schedule, and 0 means retry without sleeping."
            )
        self.retry_backoff_s = retry_backoff_s
        # Cap on a single inter-attempt delay. Exponential backoff without a
        # cap grows without bound (2**n); a degenerate but supported config
        # (high max_retries) would otherwise block the worker's event loop for
        # minutes on one call. None disables the cap for consumers that want
        # the legacy unbounded behaviour, but the default bounds it.
        if retry_backoff_max_s is not None and retry_backoff_max_s <= 0:
            raise ValueError(
                f"retry_backoff_max_s must be > 0 (got {retry_backoff_max_s}); "
                "pass None to disable the cap."
            )
        self.retry_backoff_max_s = retry_backoff_max_s
        # Optional budget on the inter-attempt sleeps one call may *start*.
        # The per-delay caps bound each sleep in isolation, but they multiply
        # against the attempt budget, and Retry-After bypasses
        # retry_backoff_max_s by design: a backend answering every attempt with
        # ``Retry-After: 21600`` pins a terminal complete/fail report (6-attempt
        # floor, so five inter-attempt sleeps) for up to 5 × 6h = 30h on one
        # call. The worker runs one task at a time, so that call blocks the
        # whole polling loop: no new claims, no cancel polls, no shutdown
        # response — the process looks hung to operators.
        #
        # It bounds admission, not wall clock. The loop refuses to *start* a
        # sleep that would not fit in what remains, and each sleep is charged
        # what the monotonic clock says it cost — but a sleep already in
        # flight is never cut short, so the real elapsed time can end up over
        # budget. See _retry.
        #
        # ponytail: soft budget — a starved event loop overruns it by however
        # long it was blocked, and the loop only notices at the next admission
        # check. Ceiling: nothing running *inside* a blocked loop can preempt
        # the sleep — an asyncio.wait_for timer is starved by the same block,
        # so a hard deadline here would be a hard deadline in name only.
        # Upgrade path: keep handler work off this loop (executor or
        # subprocess) so sleeps wake on time and admission matches wall clock.
        #
        # Default None: unbounded, exactly as before this knob existed. 600s
        # (ten minutes) is the *recommended* opt-in value — longer than any
        # real backend restart or rate-limit window we've observed, and giving
        # up after it costs nothing a retries-exhausted report doesn't already
        # cost: the task is re-queued by the sweeper either way. Enable it
        # deliberately, per consumer (see docs/fleet/runbooks/sdk-upgrade.md).
        #
        # NaN is rejected as well as <= 0: every comparison against NaN is
        # False, so a NaN budget would sail through a bare ``<= 0`` check and
        # then silently disable the very limit it was asked to impose. ``inf``
        # is an unbounded budget spelled as a number; None is the one
        # documented way to say that.
        if retry_sleep_budget_s is not None and not (
            math.isfinite(retry_sleep_budget_s) and retry_sleep_budget_s > 0
        ):
            raise ValueError(
                f"retry_sleep_budget_s must be a finite number > 0 "
                f"(got {retry_sleep_budget_s}); pass None (the default) to "
                "disable the budget."
            )
        self.retry_sleep_budget_s = retry_sleep_budget_s
        # Jitter decorrelates retries across the fleet so a shared transient
        # outage doesn't produce a synchronized retry storm the instant the
        # backend recovers. Default on; tests that assert on exact delays pass
        # retry_jitter=False.
        self.retry_jitter = retry_jitter
        # All four HTTP deadlines are validated here, before the owned
        # AsyncClient below exists: raising after constructing it would leak
        # an unclosed client (and its connection pool) on every bad config.
        _validate_timeout_s("timeout_s", timeout_s, allow_none=False)
        _validate_timeout_s("file_timeout_s", file_timeout_s, allow_none=True)
        _validate_timeout_s("cancel_timeout_s", cancel_timeout_s, allow_none=True)
        _validate_timeout_s(
            "lifecycle_timeout_s", lifecycle_timeout_s, allow_none=True,
        )
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout_s,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        # File transfers (download_file / upload_file) can move GB-scale
        # outputs (colmap-splat PLY files, Neural-Canvas splats) that take
        # minutes to stream — far longer than the 30s general request timeout
        # that governs claim/heartbeat/complete. A single ``timeout_s`` for
        # every operation meant workers hit WriteTimeout/ReadTimeout on big
        # files, exhausted retries inside the same 30s window, and failed
        # tasks that would succeed with a file-appropriate timeout. The file
        # timeout is applied per-request (see download_file / upload_file) so
        # it overrides the client default only for those calls, leaving
        # lifecycle latency (claim, heartbeat, cancel-poll) untouched. ``None``
        # falls back to the client's own timeout (legacy behaviour) for
        # consumers that supply their own client and don't want the SDK to
        # impose a separate file deadline — see _per_request_timeout for why
        # that fallback is a sentinel rather than a literal ``None``.
        self._file_timeout = _per_request_timeout(file_timeout_s)
        # Cancel polling (get_cancel_status) is a cheap read-only GET that the
        # CancelGuard fires every ``cancel_poll_interval_s`` (default 2s). It
        # used the 30s general request timeout, meaning a single slow poll
        # under backend load could block the guard for 30s (plus retry backoff)
        # — during which the worker kept computing on a task the user had
        # already cancelled. A dedicated short deadline keeps cancel detection
        # responsive: a stalled poll fails fast (caught by CancelGuard's
        # try/except), the next poll fires on schedule, and the worker learns
        # of the cancel within seconds instead of tens of seconds. The 30s
        # general timeout still governs claim; report_progress/complete/fail
        # have their own ``lifecycle_timeout_s`` deadline. ``None`` falls back
        # to the client's own timeout for consumers that don't want a separate
        # cancel deadline (see _per_request_timeout).
        self._cancel_timeout = _per_request_timeout(cancel_timeout_s)
        # Lifecycle writes (report_progress / complete / fail) are the worker's
        # terminal-ish status calls. They used the 30s general request timeout,
        # so a temporarily-slow backend could block the polling loop for up to
        # 30s × max_retries (~120s with the default 4 attempts) on a single
        # heartbeat or complete call — during which the worker can't claim new
        # work, poll for cancel, or respond to shutdown. A dedicated shorter
        # deadline (default 15s) bounds that worst case: a stalled lifecycle
        # call fails fast, the retry loop still rides through transient blips
        # (4 × 15s = 60s total instead of 4 × 30s = 120s), and the polling
        # loop stays responsive under backend load. This completes the
        # timeout-separation pattern established by cancel_timeout_s
        # (cancel-poll) and file_timeout_s (file transfers): the 30s general
        # timeout now governs only claim_next. ``None`` falls back to the
        # client's own timeout for consumers that don't want a separate
        # lifecycle deadline (see _per_request_timeout).
        self._lifecycle_timeout = _per_request_timeout(lifecycle_timeout_s)
        self._payload_logger = payload_logger

    async def __aenter__(self) -> "BackendClient":
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ----- core request with retry ------------------------------------

    async def _retry(
        self,
        fn,
        *,
        method: str,
        path: str,
        extra_transient: frozenset = frozenset(),
        attempts: Optional[int] = None,
        retry_after_max_s: Optional[int] = _MAX_RETRY_AFTER_S,
    ):
        """Run ``await fn()`` with exponential-backoff retry on transient errors.

        Shared by :meth:`_request` (buffered) and :meth:`download_file`
        (streaming).  ``fn`` is re-invoked from scratch on every attempt, so
        callers that mutate state mid-attempt (e.g. opening a file for write)
        must be idempotent — ``download_file`` opens ``dest`` with ``"wb"``
        which truncates, so a retry starts a clean file.

        Retries two classes of transient failure:

        - ``httpx.TransportError`` / ``httpx.TimeoutException`` — the request
          never reached the backend or the connection dropped.
        - ``httpx.HTTPStatusError`` whose status is a transient code
          (408/429/502/503/504) — 408 is the server reporting a transport
          timeout on a slow request (nginx's client_body_timeout on a
          GB-scale upload), 429 means the backend rate-limited the call
          (the shared backend serves the whole fleet), while 502/503/504 mean
          the gateway is up but the upstream Flask app is momentarily
          unavailable (restart, overload, deploy). Other status errors
          (500, other 4xx) surface immediately without consuming retry
          budget, matching the pre-existing non-transient pass-through contract.

        The backoff is exponential (``retry_backoff_s * 2**n``), capped at
        ``retry_backoff_max_s`` so a high ``max_retries`` can't block the
        worker for minutes on one call, and jittered (±25%) so the fleet's
        workers don't retry in lockstep and re-overload the backend the
        instant it recovers. ``max_retries`` attempts fire in total, unless
        the caller overrides the budget via ``attempts``.

        When a retryable *status* response carries a parseable ``Retry-After``
        header (delta-seconds or HTTP-date), that delay is honoured instead of
        the computed backoff — the server knows when its rate-limit window
        closes better than our schedule does — and ``Retry-After: 0`` retries
        immediately. Server guidance has its own six-hour safety ceiling; it
        is not shortened by ``retry_backoff_max_s``. When jitter is enabled,
        only positive jitter is added, so fleet workers spread out after the
        named instant without retrying early. Honouring the header changes
        only the delay, never the attempt budget.

        ``extra_transient`` widens the transient status set for this call
        only — used by ``complete``/``fail`` to also retry 500 (see the
        ``_TRANSIENT_STATUS_CODES`` comment for the rationale).

        ``retry_sleep_budget_s`` bounds the inter-attempt sleeps this call is
        allowed to *start*. It defaults to ``None`` — unbounded, the behaviour
        that predates the knob — and 600s is the recommended value to opt into.
        The per-delay caps bound each sleep in isolation but multiply against
        the attempt budget, and ``Retry-After`` deliberately bypasses
        ``retry_backoff_max_s`` — so a persistently throttled backend can hold
        one call (and with it the worker's single-task polling loop) for up to
        5 × 6h, the five inter-attempt sleeps of a terminal report's 6-attempt
        floor. Once a budget is set: when the next required delay would not fit
        in what's left of it, the loop stops early and re-raises the last error
        instead of sleeping on and firing a near-certainly-futile request
        inside the rate-limit window; that early stop is logged at WARNING. The
        outcome for the caller is identical to exhausting the attempt budget —
        for a terminal report, the task is re-queued by the backend's sweeper.

        It is a budget on admission, **not** a wall-clock deadline. Spend is
        measured on the monotonic clock, so a sleep that overruns its requested
        delay is charged what it actually cost and tightens every later
        admission — but a sleep already in flight is never interrupted, so real
        elapsed time can exceed the budget by however long the event loop was
        starved (the worker's own handler runs in this process). A 600s budget
        whose sleeps each take twice their delay stops after ~800s, not 600s.
        Bounding that last overrun is not something this loop can do from
        inside the blocked loop, and it is why this is a budget rather than a
        maximum: pick a value with headroom against a handler that blocks.
        """
        import asyncio

        total_attempts = attempts if attempts is not None else self.max_retries
        last_exc: Optional[Exception] = None
        slept_s = 0.0
        for attempt in range(total_attempts):
            try:
                return await fn()
            except _RETRYABLE_EXCEPTIONS as e:
                last_exc = e
                if attempt == total_attempts - 1:
                    break
                delay = _backoff_delay(
                    attempt, self.retry_backoff_s,
                    self.retry_backoff_max_s, self.retry_jitter,
                )
                what, source = type(e).__name__, ""
            except httpx.HTTPStatusError as e:
                if not _is_transient_status(e, extra_transient):
                    raise
                last_exc = e
                if attempt == total_attempts - 1:
                    break
                # A status response can name its own retry instant via
                # Retry-After; honour it in preference to our schedule. On a
                # 429 the backend's rate-limit window is authoritative, and
                # spending the attempt budget on the SDK's own 2s/4s/8s
                # schedule *inside* that window is how a terminal
                # complete/fail report gets dropped — the task then sits
                # in_progress until the sweeper reclaims it. Absent or
                # unparseable header → the capped-jittered exponential
                # schedule, exactly as before.
                retry_after = _retry_after_delay(e.response, maximum_seconds=retry_after_max_s)
                if retry_after is None:
                    delay = _backoff_delay(
                        attempt, self.retry_backoff_s,
                        self.retry_backoff_max_s, self.retry_jitter,
                    )
                else:
                    # Never shorten server guidance. Positive-only jitter
                    # spreads fleet retries after the named instant without
                    # moving any request back inside the closed window.
                    delay = retry_after
                    if self.retry_jitter and delay > 0:
                        if retry_after_max_s is None:
                            delay += random.uniform(0.0, 1.0)
                        else:
                            delay *= 1.0 + random.uniform(0.0, _JITTER_SPREAD)
                            delay = min(delay, retry_after_max_s)
                what = f"HTTP {e.response.status_code}"
                source = " (Retry-After)" if retry_after is not None else ""
            # Reached only from a retryable failure that still has attempts
            # left; both branches above have picked this attempt's delay.
            if (
                self.retry_sleep_budget_s is not None
                and slept_s + delay > self.retry_sleep_budget_s
            ):
                # Waiting the delay out would blow the budget, and firing the
                # request early lands inside the window the backend just named
                # — so stop here and let the caller (and the sweeper) handle
                # it. WARNING, not DEBUG: from the outside this looks like a
                # worker giving up while the backend is plainly still up. The
                # remaining figure floors at zero — an overrun sleep can leave
                # it negative, and "the remaining -200.0s" reads as a bug; the
                # elapsed figure next to it already shows the overspend.
                log.warning(
                    "transient %s on %s %s; giving up after %d attempt(s) and "
                    "%.1fs of retry backoff — the next delay (%.1fs) exceeds "
                    "the remaining %.1fs of the %.1fs retry_sleep_budget_s",
                    what, method, path, attempt + 1, slept_s, delay,
                    max(0.0, self.retry_sleep_budget_s - slept_s),
                    self.retry_sleep_budget_s,
                )
                break
            log.debug(
                "transient %s on %s %s; retrying in %.1fs%s",
                what, method, path, delay, source,
            )
            started = time.monotonic()
            await asyncio.sleep(delay)
            # Charge what the sleep *cost*, not what it asked for. A budget
            # summed from requested delays is a budget on our intentions: an
            # event loop starved by a blocking handler (the worker's own task
            # runs in this process) hands control back late, and enough of
            # those overruns blow the wall-clock budget with the arithmetic
            # still insisting it fits. Measuring narrows that gap — every later
            # admission sees the real spend — but does not close it: this
            # sleep's own overrun is already past. It can only ever charge
            # more, since asyncio.sleep never returns early and time.monotonic
            # never runs backwards, so `delay` is the floor. The requests
            # themselves stay out of the budget: each has its own timeout, and
            # download_file's minutes-long transfers would otherwise eat a
            # retry budget meant for backoff.
            slept_s += max(delay, time.monotonic() - started)
        # last_exc is guaranteed non-None here because __init__ rejects
        # max_retries < 1, so the loop always executes at least once. The
        # explicit guard avoids a bare assert (which is stripped under -O
        # and produces an opaque AssertionError otherwise) and documents
        # the invariant for readers.
        if last_exc is None:  # pragma: no cover — unreachable per __init__ guard
            raise RuntimeError(
                f"_retry completed without an attempt or exception "
                f"(attempts={total_attempts}); this is a bug."
            )
        raise last_exc

    async def _request(
        self,
        method: str,
        path: str,
        *,
        extra_transient: frozenset = frozenset(),
        attempts: Optional[int] = None,
        **kwargs,
    ) -> httpx.Response:
        """Request with exponential-backoff retry on transient errors.

        Retries ``httpx.TransportError`` / ``httpx.TimeoutException`` and
        transient status codes (408/429/502/503/504); other HTTP status
        errors surface immediately. Uses no third-party retry library to keep
        SDK dependencies minimal.

        ``extra_transient`` / ``attempts`` are forwarded to :meth:`_retry`
        (terminal reports widen the transient set to include 500 and raise
        the attempt budget); all other kwargs go to httpx.

        ``raise_for_status()`` runs *inside* the retry closure so a transient
        5xx is seen by ``_retry`` and retried. ``claim_next`` does not use this
        method — it calls ``_retry`` directly with its own closure so it can
        treat 204/404 as success variants before any status check.
        """

        async def _do_request() -> httpx.Response:
            resp = await self._client.request(method, path, **kwargs)
            resp.raise_for_status()
            return resp

        return await self._retry(
            _do_request, method=method, path=path,
            extra_transient=extra_transient, attempts=attempts,
        )

    # ----- task lifecycle --------------------------------------------

    async def resource_claim(self, journal, worker_instance_id, task_types, host_report):
        """V2 claim transport; returns (claim or None, minimum next-poll delay).

        The caller runs heartbeat/cancel independently and waits the returned
        delay. Journal identity is persisted before entering the HTTP retry loop.
        This does not enable v2 on the existing Worker loop.
        """
        from .resources import ClaimResult

        request = journal.prepare(worker_instance_id, frozenset(task_types))
        body = {**request.model_dump(mode="json"), "host_report": host_report.model_dump(mode="json")}
        response = await self._resource_request("POST", "/tasks/claim", json=body)
        if response.status_code == 204:
            journal.record_response(request.claim_request_id, None)
            delay = _retry_after_delay(response, maximum_seconds=None)
            return None, (5.0 if delay is None else delay)
        result = ClaimResult.model_validate(response.json())
        if result.ownership.worker_instance_id != worker_instance_id:
            raise ProtocolError("claim response belongs to another worker instance")
        journal.record_response(request.claim_request_id, result)
        return result, 0.0

    async def _resource_request(self, method, path, **kwargs):
        async def once():
            response = await self._client.request(method, path, timeout=self._lifecycle_timeout, **kwargs)
            if response.status_code in (410, 426):
                raise ProtocolError("worker_protocol_unsupported: coordinated worker upgrade required")
            response.raise_for_status()
            return response

        return await self._retry(once, method=method, path=path, retry_after_max_s=None)

    async def resource_operation(self, journal, kind, payload, *, host_report=None, cleanup=None):
        """Persist operation UUID once, then reuse through all transport retries."""
        from .resources import AdmissionError, ClaimResult

        if kind not in ("start", "complete", "fail", "decline", "release"):
            raise AdmissionError("invalid_operation")
        pending = journal.pending()
        if not pending or not pending[1] or not pending[1]["claim"]:
            raise AdmissionError("claim_request_unknown")
        claim = ClaimResult.model_validate(pending[1]["claim"])
        semantic_payload = payload
        if kind == "release":
            if cleanup is None or host_report is None:
                raise AdmissionError("cleanup_evidence_required")
            semantic_payload = cleanup.observation.evidence.model_dump(mode="json")
        body = {**payload, "protocol_version": 2, "ownership": claim.ownership.model_dump(mode="json")}
        if host_report is not None:
            body["host_report"] = host_report.model_dump(mode="json")
        if cleanup is not None:
            body["cleanup"] = cleanup.model_dump(mode="json")
        operation_id = journal.prepare_operation(kind, semantic_payload, request=body)
        return await self._resource_replay_operation(journal, claim, kind, operation_id)

    async def resource_recover_operations(self, journal):
        """Replay durable requests after restart; never execute a recovered handler.

        Start acknowledgement is not permission to resume an old process. The
        supervisor must reconcile that attempt before this worker can claim again.
        """
        from .resources import AdmissionError, ClaimResult

        pending = journal.pending()
        if not pending or not pending[1] or not pending[1]["claim"]:
            raise AdmissionError("claim_request_unknown")
        claim = ClaimResult.model_validate(pending[1]["claim"])
        states = []
        for kind, operation_id in journal.unresolved_operations():
            states.append(await self._resource_replay_operation(journal, claim, kind, operation_id))
        return states

    async def _resource_replay_operation(self, journal, claim, kind, operation_id):
        from .resource_protocol import AttemptState

        body = journal.operation_request(kind, operation_id)
        method = "PUT" if kind in ("complete", "fail") else "POST"
        try:
            response = await self._resource_request(method, f"/tasks/{claim.task_id}/{kind}", json=body)
        except httpx.HTTPStatusError as exc:
            # These named precondition failures roll back the backend transaction.
            # Timeouts, 5xx, fencing and idempotency conflicts remain unresolved.
            if exc.response.status_code == 409:
                try:
                    code = exc.response.json().get("code")
                except (ValueError, AttributeError):
                    code = None
                if code in ("hardware_report_stale", "hardware_report_replayed", "cleanup_evidence_stale"):
                    journal.record_operation(kind, operation_id, {"rejected": code})
            raise
        state = AttemptState.model_validate(response.json())
        if state.attempt_id != claim.ownership.attempt_id or state.task_id != claim.task_id:
            raise ProtocolError("operation response belongs to another attempt")
        journal.record_operation(kind, operation_id, state.model_dump(mode="json"))
        return state

    async def resource_progress(self, claim, progress):
        """One-shot display update; lease renewal runs independently."""
        from .resource_protocol import AttemptState

        response = await self._client.request("PUT", f"/tasks/{claim.task_id}/progress", timeout=5,
            json={"protocol_version": 2, "ownership": claim.ownership.model_dump(mode="json"), "progress": progress})
        response.raise_for_status()
        return AttemptState.model_validate(response.json())

    async def resource_status(self, claim):
        from .resource_protocol import AttemptState

        params = claim.ownership.model_dump(mode="json", exclude={"token"})
        response = await self._resource_request("GET", f"/tasks/{claim.task_id}/cancel-status",
                                                params=params, headers={"X-Attempt-Token": claim.ownership.token})
        return AttemptState.model_validate(response.json())

    async def resource_heartbeat(self, claim):
        from .resource_protocol import AttemptState

        response = await self._resource_request("POST", "/workers/heartbeat", params={"task_id": claim.task_id},
                                                json={"protocol_version": 2, "ownership": claim.ownership.model_dump(mode="json")})
        return AttemptState.model_validate(response.json())

    async def resource_ready(self, journal, worker_instance_id):
        from .resources import AdmissionError

        if journal.pending() is not None:
            raise AdmissionError("previous_claim_unresolved")
        response = await self._resource_request("POST", "/workers/ready",
            json={"protocol_version": 2, "worker_instance_id": str(worker_instance_id)})
        body = response.json()
        if body.get("worker_instance_id") != str(worker_instance_id) or body.get("ready") is not True:
            raise ProtocolError("invalid worker readiness acknowledgement")

    async def resource_download(self, claim, artifact, dest: Path):
        """Fetch exactly the admitted input and reject truncated or changed bytes."""
        import hashlib
        from .files import _require_safe_filename

        _require_safe_filename(artifact.filename, field="inputs", key="filename")
        if artifact not in claim.task.inputs.values():
            raise ProtocolError("input not declared in claim")
        path = f"/tasks/{claim.task_id}/attempts/{claim.ownership.attempt_id}/inputs/{artifact.filename}"
        params = claim.ownership.model_dump(mode="json", exclude={"token", "attempt_id"})
        params["protocol_version"] = 2

        async def once():
            digest = hashlib.sha256()
            size = 0
            async with self._client.stream("GET", path, params=params,
                    headers={"X-Attempt-Token": claim.ownership.token}, timeout=self._file_timeout) as response:
                if response.status_code in (410, 426):
                    raise ProtocolError("worker_protocol_unsupported: coordinated worker upgrade required")
                response.raise_for_status()
                file = await _to_thread_complete(open, dest, "wb", cancel_cleanup=lambda opened: opened.close())
                try:
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > artifact.size_bytes:
                            raise ProtocolError("input exceeds admitted size")
                        digest.update(chunk)
                        await _to_thread_complete(file.write, chunk)
                    if size != artifact.size_bytes or digest.hexdigest() != artifact.sha256:
                        raise ProtocolError("input differs from admitted digest")
                finally:
                    await _to_thread_complete(file.close)

        try:
            await self._retry(once, method="GET", path=path, retry_after_max_s=None)
        except BaseException:
            dest.unlink(missing_ok=True)
            raise

    async def resource_upload(self, claim, filename, src: Path):
        """Immutable attempt output; retries restart the stream, never overwrite another attempt."""
        import hashlib
        from .files import _require_safe_filename

        _require_safe_filename(filename, field="output_files", key="filename")
        path = f"/tasks/{claim.task_id}/attempts/{claim.ownership.attempt_id}/outputs/{filename}"
        params = claim.ownership.model_dump(mode="json", exclude={"token", "attempt_id"})
        params["protocol_version"] = 2

        async def once():
            digest = hashlib.sha256()
            size = 0
            file = await _to_thread_complete(open, src, "rb", cancel_cleanup=lambda opened: opened.close())
            try:
                async def chunks():
                    nonlocal size
                    while chunk := await _to_thread_complete(file.read, _DOWNLOAD_CHUNK_BYTES):
                        size += len(chunk)
                        digest.update(chunk)
                        yield chunk

                response = await self._client.request("PUT", path, params=params, content=chunks(),
                    headers={"X-Attempt-Token": claim.ownership.token, "Content-Type": "application/octet-stream"},
                    timeout=self._file_timeout)
                if response.status_code in (410, 426):
                    raise ProtocolError("worker_protocol_unsupported: coordinated worker upgrade required")
                response.raise_for_status()
                expected = {"filename": filename, "sha256": digest.hexdigest(), "size_bytes": size}
                if response.json() != expected:
                    raise ProtocolError("artifact acknowledgement differs from uploaded content")
                return expected
            finally:
                await _to_thread_complete(file.close)

        return await self._retry(once, method="PUT", path=path, retry_after_max_s=None)

    async def claim_next(
        self, task_types: list, worker_id: str
    ) -> Optional[ClaimedTask]:
        """GET /tasks/next — claim the next available task. Returns None on 204.

        On protocol-drift failures (response not parseable as JSON, or JSON
        body that fails ClaimedTask.from_dict validation) the raw response is
        recorded via the optional payload_logger before re-raising. This is
        how a worker captures evidence when the backend ships a new task
        type before the worker fleet has been upgraded.
        """
        types_str = ",".join(
            t.value if hasattr(t, "value") else str(t) for t in task_types
        )
        path = "/tasks/next"
        params = {"types": types_str, "worker_id": worker_id}

        # claim_next treats 204 (no task) and 404 (older backend without the
        # /tasks/next route) as success variants, so it can't reuse _request's
        # blanket raise_for_status. It calls _retry directly with a closure
        # that returns the response for 204/404 and raises for everything else
        # — so a transient 408/429/502/503/504 is still retried here, matching every
        # other backend call.
        async def _claim_once() -> Optional[httpx.Response]:
            resp = await self._client.request(
                "GET", path, params=params,
            )
            if resp.status_code in (204, 404):
                return resp
            resp.raise_for_status()
            return resp

        resp = await self._retry(_claim_once, method="GET", path=path)
        if resp.status_code == 204:
            return None
        if resp.status_code == 404:
            # Older backends without /tasks/next return 404; treat as no-task.
            log.warning("backend %s has no /tasks/next", self.base_url)
            return None

        try:
            body = resp.json()
        except Exception as exc:
            if self._payload_logger is not None:
                self._payload_logger.record_raw(
                    resp.text,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
            raise ProtocolError(
                f"claim_next response was not valid JSON: {resp.text[:500]!r}"
            ) from exc

        if body is None:
            return None

        try:
            return ClaimedTask.from_dict(body)
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            if self._payload_logger is not None:
                self._payload_logger.record_raw(
                    body,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
            raise ProtocolError(
                f"claim_next returned an unexpected envelope: {resp.text[:500]!r}"
            ) from exc

    async def report_progress(
        self,
        task_id: int,
        *,
        stage: str,
        current: int = 0,
        total: int = 0,
        kill_handle: Optional[dict] = None,
    ) -> dict:
        """PUT /tasks/{id}/progress — heartbeat + progress. Returns response body.

        Uses the dedicated ``lifecycle_timeout_s`` deadline (default 15s, set
        via :meth:`BackendClient.__init__`) rather than the 30s general request
        timeout. A heartbeat that stalls under backend load should fail fast
        so the polling loop stays responsive and the next heartbeat fires on
        schedule, rather than blocking the worker for up to 120s (30s × 4
        retries) on a single slow progress call.
        """
        resp = await self._request(
            "PUT", f"/tasks/{task_id}/progress",
            json=_progress_body(stage, current, total, kill_handle),
            params=self._worker_params,
            timeout=self._lifecycle_timeout,
        )
        return resp.json() or {}

    async def report_progress_once(
        self,
        task_id: int,
        *,
        stage: str,
        current: int = 0,
        total: int = 0,
        kill_handle: Optional[dict] = None,
    ) -> dict:
        """One-shot PUT /tasks/{id}/progress — no retries, same wire format.

        Unlike :meth:`report_progress`, this performs a *single* HTTP request
        with no exponential-backoff retry. It exists for the caller on a
        handler's critical path: :meth:`ProgressReporter.update
        <task_worker_api.progress.ProgressReporter.update>` emits an immediate
        report on every stage transition, and going through ``_retry`` meant a
        degraded backend could stall the handler for ``max_retries`` ×
        ``lifecycle_timeout_s`` plus backoff sleeps (~75s with the default 4
        attempts, 15s timeout, 2s base backoff) on one progress update — while
        the work the update was describing sat idle.

        Dropping a *single* progress report is cheap: the state it carries is
        kept in the reporter and re-sent by the next background heartbeat,
        which still uses the retried :meth:`report_progress` so the task's
        ``updated_at`` keeps riding through backend blips (the sweeper reads a
        stale ``updated_at`` as abandonment). Stage-transition latency is the
        only thing traded away, and only while the backend is degraded.

        Transport errors and transient HTTP status codes surface immediately
        rather than being retried; ``ProgressReporter.update`` catches them and
        logs at WARNING, exactly as it already does when the retried call
        exhausts its budget.

        Uses the same dedicated ``lifecycle_timeout_s`` deadline as
        :meth:`report_progress`.
        """
        resp = await self._client.request(
            "PUT", f"/tasks/{task_id}/progress",
            json=_progress_body(stage, current, total, kill_handle),
            params=self._worker_params,
            timeout=self._lifecycle_timeout,
        )
        resp.raise_for_status()
        return resp.json() or {}

    async def get_box_id(self) -> Optional[str]:
        """GET /tasks/box-id — this backend's box identity.

        Used by the worker's volume-affinity check when cross-box targets are
        configured. Returns ``None`` on a backend that predates the endpoint
        (404), so callers can warn-and-continue instead of failing rollout
        ordering.
        """
        try:
            resp = await self._request("GET", "/tasks/box-id")
        except httpx.HTTPStatusError as e:
            if e.response is not None and e.response.status_code == 404:
                return None
            raise
        return (resp.json() or {}).get("box_id")

    async def get_cancel_status(self, task_id: int) -> dict:
        """GET /tasks/{id}/cancel-status — cheap read-only cancel check.

        Uses the dedicated ``cancel_timeout_s`` deadline (default 5s, set via
        :meth:`BackendClient.__init__`) rather than the 30s general request
        timeout. The CancelGuard polls this endpoint every few seconds; a
        single slow poll under backend load would otherwise block cancel
        detection for 30s (plus retry backoff), keeping the worker blind to a
        user cancel. The short deadline fails fast — CancelGuard catches the
        timeout and the next poll fires on schedule.

        .. note::
            This method retries transient errors (transport errors and 408/
            429/502/503/504) via the standard ``_retry`` loop. For the
            CancelGuard's hot poll path, prefer :meth:`poll_cancel_status`,
            which is a single-shot GET with no retries — the guard has its
            own poll-interval retry and the client-level backoff chain
            (up to ~50s with default settings) only delays cancel detection.
        """
        resp = await self._request(
            "GET", f"/tasks/{task_id}/cancel-status",
            timeout=self._cancel_timeout,
        )
        return resp.json() or {}

    async def poll_cancel_status(self, task_id: int) -> dict:
        """One-shot GET /tasks/{id}/cancel-status — no retries, short timeout.

        Unlike :meth:`get_cancel_status`, this method performs a *single*
        HTTP request with no exponential-backoff retry. The
        :class:`~task_worker_api.cancel.CancelGuard` polls this endpoint on
        its own schedule (``cancel_poll_interval_s``, default 2s); layering
        the client's ``_retry`` loop on top meant a degraded backend could
        blind the guard for up to ``max_retries`` × ``cancel_timeout_s`` plus
        backoff sleeps (~50s with default 4 attempts, 5s timeout, 2s base
        backoff) — during which the worker kept computing on a cancelled
        task. The one-shot call fails fast (within ``cancel_timeout_s``,
        default 5s): transport errors and transient HTTP status codes surface
        immediately so the guard catches them at DEBUG and retries on its own
        next tick.

        Non-transient HTTP status errors (4xx/500) are raised immediately,
        matching :meth:`get_cancel_status`'s non-transient contract.

        Uses the same dedicated ``cancel_timeout_s`` deadline as
        :meth:`get_cancel_status`.
        """
        resp = await self._client.request(
            "GET", f"/tasks/{task_id}/cancel-status",
            timeout=self._cancel_timeout,
        )
        resp.raise_for_status()
        return resp.json() or {}

    async def complete(self, task_id: int, result: dict) -> None:
        """PUT /tasks/{id}/complete — final success payload.

        Uses the dedicated ``lifecycle_timeout_s`` deadline (default 15s) so
        a stalled complete call fails fast instead of blocking the polling
        loop for up to 120s (30s × 4 retries) under backend load.

        Terminal reports retry harder than other calls: 500 is treated as
        transient (a dead backend dependency mid-write looks like a 500 here,
        and dropping the report orphans the computed outcome) and the attempt
        budget is raised to at least ``_TERMINAL_MIN_ATTEMPTS`` so the retry
        window (~60s jittered) rides out a backend restart.
        """
        await self._request(
            "PUT", f"/tasks/{task_id}/complete", json={"result": result},
            params=self._worker_params,
            timeout=self._lifecycle_timeout,
            extra_transient=_TERMINAL_EXTRA_TRANSIENT,
            attempts=max(self.max_retries, _TERMINAL_MIN_ATTEMPTS),
        )

    async def fail(self, task_id: int, error: str) -> None:
        """PUT /tasks/{id}/fail — final failure payload.

        Uses the dedicated ``lifecycle_timeout_s`` deadline (default 15s) so
        a stalled fail call fails fast instead of blocking the polling loop
        for up to 120s (30s × 4 retries) under backend load.

        Retries 500 with a raised attempt budget, same as :meth:`complete` —
        see there for the rationale.

        ``error`` is capped at ``_MAX_FAIL_ERROR_BYTES`` (see
        :func:`_cap_fail_error`) — the cap lives here, on the wire boundary
        that owns the constraint, so it covers every caller.
        """
        await self._request(
            "PUT", f"/tasks/{task_id}/fail", json={"error": _cap_fail_error(error)},
            params=self._worker_params,
            timeout=self._lifecycle_timeout,
            extra_transient=_TERMINAL_EXTRA_TRANSIENT,
            attempts=max(self.max_retries, _TERMINAL_MIN_ATTEMPTS),
        )

    # ----- file transfer (remote mode workers) ----------------------

    async def download_file(
        self, task_id: int, filename: str, dest: Path,
        *,
        cancelled: Optional["asyncio.Event"] = None,
    ) -> None:
        """GET /tasks/{id}/files/{filename} — streams to disk in 1 MB chunks.

        Retries on the same transient errors as every other backend call
        (``httpx.TransportError`` / ``httpx.TimeoutException``, plus transient
        status codes 408/429/502/503/504).  Each attempt re-opens ``dest``
        with ``"wb"`` (truncating), so a retry after a mid-stream failure
        writes a clean file rather than appending to a partial one.  A
        non-transient HTTP status error (e.g. 404/500) is raised immediately
        without consuming retry budget, matching :meth:`_request`.

        Uses the separate ``file_timeout_s`` deadline (default 300s, set via
        :meth:`BackendClient.__init__`) rather than the 30s general request
        timeout — GB-scale outputs can take minutes to stream, and the general
        timeout would spuriously abort large downloads.

        Opening, writing and closing ``dest`` all run through
        :func:`asyncio.to_thread`, mirroring ``files._copyfile_async``. They
        used to run inline on the event-loop thread, once per wire chunk: a
        multi-GB input (a colmap-splat PLY, a Neural-Canvas splat) landing on
        slow or network-mounted storage froze the loop for the whole transfer,
        so the heartbeat stopped ticking (the backend's stale-task sweeper
        reads the frozen ``updated_at`` as abandonment and reclaims a task the
        worker is actively downloading for), the ``CancelGuard`` poll froze
        with it, and a hybrid-mode FastAPI app stopped serving. Wire chunks
        are accumulated into a ``_DOWNLOAD_CHUNK_BYTES`` (1 MB) buffer before
        each write, so the thread dispatches stay proportional to the file
        size rather than to the transport's ~64 KB chunking.

        If the download does not finish — retries exhausted, a non-retryable
        error, a cancel, or the *caller* being cancelled (worker shutdown, or
        the task watchdog unwinding a run) — any partial file left at ``dest``
        is removed so callers never see a truncated/stale artifact. The
        caller-cancelled case is why the cleanup catches ``BaseException``
        rather than ``Exception``: ``asyncio.CancelledError`` is not an
        ``Exception``, and ``prepare_inputs`` stages into a stable per-task
        input dir, so a truncated file surviving a shutdown is a file a
        retried task can pick up as a complete input. Same contract, and the
        same reasoning, as ``files._copyfile_async``.

        When ``cancelled`` is supplied (an ``asyncio.Event`` from a
        ``CancelGuard`` active during input staging), the event is checked
        before the request goes out and again on every chunk the transport
        delivers, raising :class:`TaskCancelled` at that chunk boundary
        rather than waiting for the 1 MB write buffer to fill. Without
        the in-stream check, a cancel arriving mid-download was invisible
        until the whole file finished: ``prepare_inputs`` only looked between
        batch files, so a single-file input set (a lone colmap-splat PLY, a
        Neural-Canvas splat) streamed multi-GB to completion after the user
        had already cancelled. ``TaskCancelled`` is not a transient error, so
        it propagates out of the retry loop immediately without consuming
        retry budget, and the partial file at ``dest`` is cleaned up by the
        same path as any other failure.
        """
        path = f"/tasks/{task_id}/files/{filename}"
        cancel_message = (
            f"task {task_id} cancelled by user while downloading {filename}"
        )

        def _raise_if_cancelled() -> None:
            if cancelled is not None and cancelled.is_set():
                raise TaskCancelled(cancel_message)

        async def _stream_once() -> None:
            _raise_if_cancelled()
            async with self._client.stream(
                "GET", path, params=self._worker_params,
                timeout=self._file_timeout,
            ) as resp:
                resp.raise_for_status()
                f = await _to_thread_complete(
                    open, dest, "wb", cancel_cleanup=lambda opened: opened.close(),
                )
                try:
                    # The stream is iterated at the transport's own
                    # granularity (~64 KB) so ``cancelled`` is seen at every
                    # chunk that arrives; the writes are what get batched into
                    # _DOWNLOAD_CHUNK_BYTES buffers. Re-chunking the iteration
                    # itself instead would tie the cancel check to the buffer
                    # filling up: a stalled response, or a body smaller than
                    # one buffer, hides the cancel until the transfer ends.
                    buf = bytearray()
                    async for chunk in resp.aiter_bytes():
                        _raise_if_cancelled()
                        buf += chunk
                        if len(buf) >= _DOWNLOAD_CHUNK_BYTES:
                            await _to_thread_complete(f.write, buf)
                            buf = bytearray()
                    if buf:
                        await _to_thread_complete(f.write, buf)
                finally:
                    # close() flushes the last buffered write, so it blocks
                    # like any other write and belongs off the loop too.
                    await _to_thread_complete(f.close)

        operation = self._retry(_stream_once, method="GET", path=path)
        try:
            if cancelled is None:
                await operation
            else:
                await _await_unless_cancelled(
                    operation, cancelled, cancel_message,
                )
        except BaseException:
            # A mid-stream transport failure, a cancel, or the caller being
            # cancelled can leave a partial file at dest (each retry truncates
            # via "wb", but the final unfinished attempt's partial content
            # survives). Remove it so an unfinished download never leaves a
            # truncated/stale artifact behind. FileNotFoundError is an OSError,
            # so the one clause covers the already-absent case too.
            try:
                dest.unlink()
            except OSError:
                pass
            raise

    async def upload_file(
        self, task_id: int, filename: str, src: Path,
        *,
        cancelled: Optional["asyncio.Event"] = None,
    ) -> None:
        """PUT /tasks/{id}/files/{filename} — multipart upload.

        Retries on the same transient errors as every other backend call
        (``httpx.TransportError`` / ``httpx.TimeoutException``, plus transient
        status codes 408/429/502/503/504).  The body generator is created
        **inside** the per-attempt closure and opens the source file itself,
        so each retry gets a fresh handle starting at byte 0 — opening it
        once outside the loop would exhaust the handle on the first attempt
        and send zero bytes on every subsequent retry (silent data
        corruption).  A non-transient HTTP status error (e.g. 404/500) is
        raised immediately without consuming retry budget, matching
        :meth:`_request`.

        The file is read off the event-loop thread in
        ``_UPLOAD_CHUNK_BYTES`` (1 MB) chunks, mirroring what
        :meth:`download_file` does for its writes. Handing the open file
        object to httpx's ``files=`` instead put the reads *on* the loop:
        ``MultipartStream.__aiter__`` iterates the encoder synchronously and
        reads 64 KB per chunk, so a multi-GB output (a colmap-splat PLY, a
        Neural-Canvas splat) froze the loop for the whole upload — the
        heartbeat stopped ticking (the backend's stale-task sweeper reads the
        frozen ``updated_at`` as abandonment and reclaims a task the worker
        is actively uploading), the ``CancelGuard`` poll froze with it, and a
        hybrid-mode FastAPI app stopped serving. The wire bytes are
        unchanged: the multipart framing around the file is still rendered by
        httpx (see :func:`_multipart_frame`), and the explicit Content-Length
        keeps the request identity-framed rather than chunked.

        Uses the separate ``file_timeout_s`` deadline (default 300s, set via
        :meth:`BackendClient.__init__`) rather than the 30s general request
        timeout — uploading GB-scale outputs can take minutes, and the general
        timeout would spuriously abort large uploads mid-stream.

        When ``cancelled`` is supplied (an ``asyncio.Event`` from a
        ``CancelGuard`` active through the upload phase), the event is checked
        before the request goes out and the in-flight PUT is raced against it,
        so a cancel arriving mid-upload aborts the request and raises
        :class:`TaskCancelled` instead of streaming the rest of the body.
        Without the in-flight race, a cancel during an upload was invisible
        until the whole file finished: ``upload_outputs`` only looked between
        batch files, so a single-file output set (a lone colmap-splat PLY, a
        Neural-Canvas splat) streamed multi-GB to completion after the user
        had already cancelled. ``TaskCancelled`` is not a transient error, so
        it propagates out of the retry loop immediately without consuming
        retry budget — a retried cancel would just re-send the same file.
        This stops the client transport; it cannot retract a file the backend
        finished committing before the connection was severed.
        """
        path = f"/tasks/{task_id}/files/{filename}"
        cancel_message = (
            f"task {task_id} cancelled by user while uploading {filename}"
        )

        content_type, prologue, epilogue = _multipart_frame("file", filename)

        async def _upload_once() -> None:
            import asyncio

            if cancelled is not None and cancelled.is_set():
                raise TaskCancelled(cancel_message)
            size = (await asyncio.to_thread(src.stat)).st_size
            # aclosing, not a bare `content=_multipart_file_body(...)`: httpx
            # never closes a body iterator it did not create, so a transport
            # failing mid-PUT leaves the generator parked on its `yield` with
            # src open, and nothing reaps it — `_retry` holds the attempt's
            # exception in `last_exc` to re-raise, and that traceback keeps the
            # generator frame reachable. One leaked descriptor per failed
            # attempt, on exactly the multi-GB uploads most likely to retry.
            # aclose() on an exhausted generator is a no-op, so the success
            # path is unchanged.
            async with aclosing(
                _multipart_file_body(src, prologue, epilogue, size)
            ) as body:
                resp = await self._client.request(
                    "PUT", path,
                    content=body,
                    params=self._worker_params,
                    headers={
                        "Content-Type": content_type,
                        "Content-Length": str(len(prologue) + size + len(epilogue)),
                    },
                    timeout=self._file_timeout,
                )
                resp.raise_for_status()

        operation = self._retry(_upload_once, method="PUT", path=path)
        if cancelled is None:
            await operation
        else:
            await _await_unless_cancelled(
                operation, cancelled, cancel_message,
            )
