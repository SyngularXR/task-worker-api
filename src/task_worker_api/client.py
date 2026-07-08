"""Async HTTP client for the SynPusher worker protocol.

Thin wrapper over ``httpx.AsyncClient`` with retry-on-transient-error.
The wire format (paths, methods, bodies) is intentionally identical to
the pre-SDK shape — this client consolidates three divergent copies
(Blender-CLI, Neural-Canvas, colmap-splat) into one reviewed place.
"""
from __future__ import annotations

import logging
import random
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

import httpx

from .context import ClaimedTask
from .errors import ProtocolError

if TYPE_CHECKING:
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
# 500 is intentionally excluded: a 500 is the application's own error
# response, which usually signals a logic bug or a bad payload, not a
# transient outage — retrying it just burns budget and re-logs the same error.
# 4xx is excluded for the same reason (client error, retrying won't help).
_TRANSIENT_STATUS_CODES = frozenset({502, 503, 504})

# Default ceiling for a single retry delay. Without a cap, backoff grows as
# ``retry_backoff_s * 2**n`` — unbounded. A worker configured with the
# (supported) ``max_retries=8`` and the default ``retry_backoff_s=2.0`` would
# wait 256s on the 7th retry, blocking its event loop for ~10 minutes on a
# single claim/complete call. The cap keeps individual delays sane while still
# allowing long total retry windows across many attempts. Consumers can raise
# it via ``retry_backoff_max_s`` if they genuinely want longer waits.
_DEFAULT_BACKOFF_MAX_S = 60.0

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


def _is_transient_status(exc: httpx.HTTPStatusError) -> bool:
    """True iff a status error's code is a transiently-retryable gateway code."""
    return exc.response.status_code in _TRANSIENT_STATUS_CODES


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
        max_retries: int = 4,
        retry_backoff_s: float = 2.0,
        retry_backoff_max_s: float = _DEFAULT_BACKOFF_MAX_S,
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
        self._payload_logger = payload_logger

    async def __aenter__(self) -> "BackendClient":
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ----- core request with retry ------------------------------------

    async def _retry(self, fn, *, method: str, path: str):
        """Run ``await fn()`` with exponential-backoff retry on transient errors.

        Shared by :meth:`_request` (buffered) and :meth:`download_file`
        (streaming).  ``fn`` is re-invoked from scratch on every attempt, so
        callers that mutate state mid-attempt (e.g. opening a file for write)
        must be idempotent — ``download_file`` opens ``dest`` with ``"wb"``
        which truncates, so a retry starts a clean file.

        Retries two classes of transient failure:

        - ``httpx.TransportError`` / ``httpx.TimeoutException`` — the request
          never reached the backend or the connection dropped.
        - ``httpx.HTTPStatusError`` whose status is a transient gateway code
          (502/503/504) — the gateway is up but the upstream Flask app is
          momentarily unavailable (restart, overload, deploy). Other status
          errors (500, 4xx) surface immediately without consuming retry
          budget, matching the pre-existing non-transient pass-through contract.

        The backoff is exponential (``retry_backoff_s * 2**n``), capped at
        ``retry_backoff_max_s`` so a high ``max_retries`` can't block the
        worker for minutes on one call, and jittered (±25%) so the fleet's
        workers don't retry in lockstep and re-overload the backend the
        instant it recovers. ``max_retries`` attempts fire in total.
        """
        import asyncio

        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                return await fn()
            except _RETRYABLE_EXCEPTIONS as e:
                last_exc = e
                if attempt == self.max_retries - 1:
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
                if not _is_transient_status(e):
                    raise
                last_exc = e
                if attempt == self.max_retries - 1:
                    break
                delay = _backoff_delay(
                    attempt, self.retry_backoff_s,
                    self.retry_backoff_max_s, self.retry_jitter,
                )
                log.debug(
                    "transient HTTP %s on %s %s; retrying in %.1fs",
                    e.response.status_code, method, path, delay,
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
                f"(max_retries={self.max_retries}); this is a bug."
            )
        raise last_exc

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Request with exponential-backoff retry on transient errors.

        Retries ``httpx.TransportError`` / ``httpx.TimeoutException`` and
        transient 5xx gateway status codes (502/503/504); other HTTP status
        errors surface immediately. Uses no third-party retry library to keep
        SDK dependencies minimal.

        ``raise_for_status()`` runs *inside* the retry closure so a transient
        5xx is seen by ``_retry`` and retried. ``claim_next`` does not use this
        method — it calls ``_retry`` directly with its own closure so it can
        treat 204/404 as success variants before any status check.
        """

        async def _do_request() -> httpx.Response:
            resp = await self._client.request(method, path, **kwargs)
            resp.raise_for_status()
            return resp

        return await self._retry(_do_request, method=method, path=path)

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
        # — so a transient 502/503/504 is still retried here, matching every
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
        """PUT /tasks/{id}/progress — heartbeat + progress. Returns response body."""
        body: dict[str, Any] = {
            "stage": stage, "current": current, "total": total,
        }
        if kill_handle is not None:
            body["kill_handle"] = kill_handle
        resp = await self._request(
            "PUT", f"/tasks/{task_id}/progress", json=body,
        )
        return resp.json() or {}

    async def get_cancel_status(self, task_id: int) -> dict:
        """GET /tasks/{id}/cancel-status — cheap read-only cancel check."""
        resp = await self._request(
            "GET", f"/tasks/{task_id}/cancel-status",
        )
        return resp.json() or {}

    async def complete(self, task_id: int, result: dict) -> None:
        """PUT /tasks/{id}/complete — final success payload."""
        await self._request(
            "PUT", f"/tasks/{task_id}/complete", json={"result": result},
        )

    async def fail(self, task_id: int, error: str) -> None:
        """PUT /tasks/{id}/fail — final failure payload."""
        await self._request(
            "PUT", f"/tasks/{task_id}/fail", json={"error": error},
        )

    # ----- file transfer (remote mode workers) ----------------------

    async def download_file(
        self, task_id: int, filename: str, dest: Path,
    ) -> None:
        """GET /tasks/{id}/files/{filename} — streams to disk in 1 MB chunks.

        Retries on the same transient errors as every other backend call
        (``httpx.TransportError`` / ``httpx.TimeoutException``, plus transient
        5xx gateway status codes 502/503/504).  Each attempt re-opens ``dest``
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
        """
        path = f"/tasks/{task_id}/files/{filename}"

        async def _stream_once() -> None:
            async with self._client.stream(
                "GET", path, timeout=self._file_timeout,
            ) as resp:
                resp.raise_for_status()
                with open(dest, "wb") as f:
                    async for chunk in resp.aiter_bytes():
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
    ) -> None:
        """PUT /tasks/{id}/files/{filename} — multipart upload.

        Retries on the same transient errors as every other backend call
        (``httpx.TransportError`` / ``httpx.TimeoutException``, plus transient
        5xx gateway status codes 502/503/504).  The source file is opened
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
        """
        path = f"/tasks/{task_id}/files/{filename}"

        async def _upload_once() -> None:
            with open(src, "rb") as f:
                files = {"file": (filename, f)}
                resp = await self._client.request(
                    "PUT", path, files=files, timeout=self._file_timeout,
                )
                resp.raise_for_status()

        await self._retry(_upload_once, method="PUT", path=path)
