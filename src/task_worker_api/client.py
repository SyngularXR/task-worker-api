"""Async HTTP client for the SynPusher worker protocol.

Thin wrapper over ``httpx.AsyncClient`` with retry-on-transient-error.
The wire format (paths, methods, bodies) is intentionally identical to
the pre-SDK shape — this client consolidates three divergent copies
(Blender-CLI, Neural-Canvas, colmap-splat) into one reviewed place.
"""
from __future__ import annotations

import logging
import random
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
# 500 is intentionally excluded: a 500 is the application's own error
# response, which usually signals a logic bug or a bad payload, not a
# transient outage — retrying it just burns budget and re-logs the same error.
# Other 4xx codes are excluded for the same reason (client error, retrying
# won't help).
#
# Exception: terminal reports (``complete``/``fail``) opt in to retrying 500
# via ``_retry``'s ``extra_transient`` parameter. A 500 there can also mean
# the backend's own dependency died mid-write (e.g. Postgres I/O error), and
# dropping the report orphans a fully computed outcome — the task stays
# RUNNING until the sweeper reclaims it. Both terminal routes are idempotent
# guarded transitions, so re-PUTting is safe.
_TRANSIENT_STATUS_CODES = frozenset({429, 502, 503, 504})

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


def _is_transient_status(
    exc: httpx.HTTPStatusError,
    extra: frozenset = frozenset(),
) -> bool:
    """True iff a status error's code is a transiently-retryable code."""
    return exc.response.status_code in _TRANSIENT_STATUS_CODES or (
        exc.response.status_code in extra
    )


def _retry_after_delay(response: httpx.Response) -> Optional[float]:
    """Parse a response's ``Retry-After`` header into a delay in seconds.

    RFC 9110 allows two forms and both are accepted: delta-seconds
    (``Retry-After: 30``) and an HTTP-date (``Retry-After: Wed, 21 Oct 2026
    07:28:00 GMT``, converted to a delay relative to now).

    Returns ``None`` only when the header carries no usable guidance — absent
    or malformed. The caller then falls back to its own exponential schedule.
    Valid delays are capped at ``_MAX_RETRY_AFTER_S``. This is deliberately
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
        ceiling = str(_MAX_RETRY_AFTER_S)
        if len(value) > len(ceiling) or (
            len(value) == len(ceiling) and value > ceiling
        ):
            return float(_MAX_RETRY_AFTER_S)
        return float(value)
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
    return min(delay, _MAX_RETRY_AFTER_S)


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
    - **Jitter**: when enabled, the (capped) delay is multiplied by a uniform
      random factor in ``[1 - _JITTER_SPREAD, 1 + _JITTER_SPREAD]``. The three
      fleet workers share one backend; without jitter they'd all retry on the
      identical deterministic schedule and re-overload it the instant it
      recovers (thundering herd). Jitter decorrelates them.

    ``rng`` is injectable so tests can assert on exact delays deterministically.
    """
    delay = base_s * (2**attempt)
    if max_s is not None and delay > max_s:
        delay = max_s
    if jitter and delay > 0:
        r = rng if rng is not None else random
        factor = 1.0 + r.uniform(-_JITTER_SPREAD, _JITTER_SPREAD)
        delay *= factor
    return delay


class BackendClient:
    """Async HTTP client bound to one SynPusher backend URL + one worker key.

    Usage:
        async with BackendClient(url, api_key) as client:
            task = await client.claim_next(types, worker_id="...")
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout_s: float = 30.0,
        file_timeout_s: Optional[float] = None,
        cancel_timeout_s: Optional[float] = 5.0,
        lifecycle_timeout_s: Optional[float] = 15.0,
        max_retries: int = 4,
        retry_backoff_s: float = 2.0,
        retry_backoff_max_s: Optional[float] = _DEFAULT_BACKOFF_MAX_S,
        retry_jitter: bool = True,
        client: Optional[httpx.AsyncClient] = None,
        payload_logger: Optional["PayloadLogger"] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
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
        # Jitter decorrelates retries across the fleet so a shared transient
        # outage doesn't produce a synchronized retry storm the instant the
        # backend recovers. Default on; tests that assert on exact delays pass
        # retry_jitter=False.
        self.retry_jitter = retry_jitter
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
        # impose a separate file deadline.
        self._file_timeout: Optional[httpx.Timeout] = (
            httpx.Timeout(file_timeout_s) if file_timeout_s is not None else None
        )
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
        # cancel deadline.
        self._cancel_timeout: Optional[httpx.Timeout] = (
            httpx.Timeout(cancel_timeout_s) if cancel_timeout_s is not None else None
        )
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
        # lifecycle deadline.
        self._lifecycle_timeout: Optional[httpx.Timeout] = (
            httpx.Timeout(lifecycle_timeout_s) if lifecycle_timeout_s is not None else None
        )
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
          (429/502/503/504) — 429 means the backend rate-limited the call
          (the shared backend serves the whole fleet), while 502/503/504 mean
          the gateway is up but the upstream Flask app is momentarily
          unavailable (restart, overload, deploy). Other status errors
          (500, non-429 4xx) surface immediately without consuming retry
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
        """
        import asyncio

        total_attempts = attempts if attempts is not None else self.max_retries
        last_exc: Optional[Exception] = None
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
                log.debug(
                    "transient %s on %s %s; retrying in %.1fs",
                    type(e).__name__, method, path, delay,
                )
                await asyncio.sleep(delay)
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
                retry_after = _retry_after_delay(e.response)
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
                        delay *= 1.0 + random.uniform(0.0, _JITTER_SPREAD)
                        delay = min(delay, _MAX_RETRY_AFTER_S)
                log.debug(
                    "transient HTTP %s on %s %s; retrying in %.1fs%s",
                    e.response.status_code, method, path, delay,
                    " (Retry-After)" if retry_after is not None else "",
                )
                await asyncio.sleep(delay)
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
        transient status codes (429/502/503/504); other HTTP status
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
        # — so a transient 429/502/503/504 is still retried here, matching every
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
        body: dict[str, Any] = {
            "stage": stage, "current": current, "total": total,
        }
        if kill_handle is not None:
            body["kill_handle"] = kill_handle
        resp = await self._request(
            "PUT", f"/tasks/{task_id}/progress", json=body,
            timeout=self._lifecycle_timeout,
        )
        return resp.json() or {}

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
            This method retries transient errors (transport errors and 429/
            502/503/504) via the standard ``_retry`` loop. For the
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
        """
        await self._request(
            "PUT", f"/tasks/{task_id}/fail", json={"error": error},
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
        status codes 429/502/503/504).  Each attempt re-opens ``dest``
        with ``"wb"`` (truncating), so a retry after a mid-stream failure
        writes a clean file rather than appending to a partial one.  A
        non-transient HTTP status error (e.g. 404/500) is raised immediately
        without consuming retry budget, matching :meth:`_request`.

        Uses the separate ``file_timeout_s`` deadline (default 300s, set via
        :meth:`BackendClient.__init__`) rather than the 30s general request
        timeout — GB-scale outputs can take minutes to stream, and the general
        timeout would spuriously abort large downloads.

        If every attempt fails (retries exhausted or a non-retryable error),
        any partial file left at ``dest`` is removed so callers never see a
        truncated/stale artifact from a failed download.

        When ``cancelled`` is supplied (an ``asyncio.Event`` from a
        ``CancelGuard`` active during input staging), the event is checked
        before the request goes out and again before each chunk is written,
        raising :class:`TaskCancelled` at the next chunk boundary. Without
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

        def _raise_if_cancelled() -> None:
            if cancelled is not None and cancelled.is_set():
                raise TaskCancelled(
                    f"task {task_id} cancelled by user while downloading "
                    f"{filename}"
                )

        async def _stream_once() -> None:
            _raise_if_cancelled()
            async with self._client.stream(
                "GET", path, timeout=self._file_timeout,
            ) as resp:
                resp.raise_for_status()
                with open(dest, "wb") as f:
                    async for chunk in resp.aiter_bytes():
                        _raise_if_cancelled()
                        f.write(chunk)

        try:
            await self._retry(_stream_once, method="GET", path=path)
        except Exception:
            # A mid-stream transport failure can leave a partial file at dest
            # (each retry truncates via "wb", but the final failed attempt's
            # partial content survives). Remove it so a failed download never
            # leaves a truncated/stale artifact behind.
            try:
                dest.unlink()
            except (FileNotFoundError, OSError):
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
        status codes 429/502/503/504).  The source file is opened
        **inside** the per-attempt closure, so each retry gets a fresh handle
        starting at byte 0 — opening it once outside the loop would exhaust
        the handle on the first attempt and send zero bytes on every
        subsequent retry (silent data corruption).  A non-transient HTTP
        status error (e.g. 404/500) is raised immediately without consuming
        retry budget, matching :meth:`_request`.

        Uses the separate ``file_timeout_s`` deadline (default 300s, set via
        :meth:`BackendClient.__init__`) rather than the 30s general request
        timeout — uploading GB-scale outputs can take minutes, and the general
        timeout would spuriously abort large uploads mid-stream.

        When ``cancelled`` is supplied (an ``asyncio.Event`` from a
        ``CancelGuard`` active during output publishing), the event is
        checked before the request goes out and the request is raced against
        it: if the event fires while the upload is still streaming, the
        in-flight request is aborted and :class:`TaskCancelled` is raised
        instead of streaming the whole body to completion. Without the
        mid-stream abort, a cancel arriving during a single-file output set
        (a lone colmap-splat PLY, a Neural-Canvas splat) was invisible until
        the entire multi-GB file finished uploading — ``upload_outputs``
        only looked between batch files, so the worker kept pushing bytes to
        a task the user had already cancelled. ``TaskCancelled`` is not a
        transient error, so it propagates out of the retry loop immediately
        without consuming retry budget (a retried cancel would re-stream the
        same file). The wire shape is unchanged: the request is built
        exactly as before (multipart with a known content length); aborting
        only closes the in-flight connection, and httpx discards the broken
        connection so the client stays usable for the next call.
        """
        import asyncio

        path = f"/tasks/{task_id}/files/{filename}"

        def _raise_if_cancelled() -> None:
            if cancelled is not None and cancelled.is_set():
                raise TaskCancelled(
                    f"task {task_id} cancelled by user while uploading "
                    f"{filename}"
                )

        async def _upload_once() -> None:
            _raise_if_cancelled()
            with open(src, "rb") as f:
                files = {"file": (filename, f)}
                if cancelled is None:
                    # No cancel event — plain request, exactly as before.
                    resp = await self._client.request(
                        "PUT", path, files=files, timeout=self._file_timeout,
                    )
                    resp.raise_for_status()
                    return
                # Race the request against the cancel event. httpx streams
                # the multipart body directly from the file handle, so there
                # is no per-chunk callback where a mid-stream cancel could
                # be observed; cancelling the request task instead aborts the
                # in-flight write at the next socket await, closing the
                # connection and unblocking the file handle via the ``with``.
                request_task = asyncio.create_task(
                    self._client.request(
                        "PUT", path, files=files, timeout=self._file_timeout,
                    )
                )
                cancel_waiter = asyncio.create_task(cancelled.wait())
                try:
                    done, _ = await asyncio.wait(
                        {request_task, cancel_waiter},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                except BaseException:
                    # The attempt itself was cancelled (e.g. worker
                    # shutdown): don't leak the two children.
                    request_task.cancel()
                    cancel_waiter.cancel()
                    await asyncio.gather(
                        request_task, cancel_waiter, return_exceptions=True,
                    )
                    raise
                if request_task in done:
                    # The upload finished first — the cancel event either
                    # never fired or fired too late to matter.
                    cancel_waiter.cancel()
                    try:
                        await cancel_waiter
                    except asyncio.CancelledError:
                        pass
                    resp = request_task.result()
                    resp.raise_for_status()
                    return
                # The cancel event fired while the request was streaming:
                # abort the upload and surface the cancel. Not a transient
                # error, so ``_retry`` re-raises it immediately without
                # spending attempt budget.
                request_task.cancel()
                try:
                    await request_task
                except asyncio.CancelledError:
                    pass
                raise TaskCancelled(
                    f"task {task_id} cancelled by user while uploading "
                    f"{filename}"
                )

        await self._retry(_upload_once, method="PUT", path=path)
