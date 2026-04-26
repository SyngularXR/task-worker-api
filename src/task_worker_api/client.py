"""Async HTTP client for the SynPusher worker protocol.

Thin wrapper over ``httpx.AsyncClient`` with retry-on-transient-error.
The wire format (paths, methods, bodies) is intentionally identical to
the pre-SDK shape — this client consolidates three divergent copies
(Blender-CLI, Neural-Canvas, colmap-splat) into one reviewed place.
"""
from __future__ import annotations

import logging
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
        max_retries: int = 4,
        retry_backoff_s: float = 2.0,
        client: Optional[httpx.AsyncClient] = None,
        payload_logger: Optional["PayloadLogger"] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.max_retries = max_retries
        self.retry_backoff_s = retry_backoff_s
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout_s,
            headers={"Authorization": f"Bearer {api_key}"},
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

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Request with exponential-backoff retry on transient transport errors.

        Uses no third-party retry library to keep SDK dependencies minimal.
        The backoff is deterministic and bounded — ``max_retries`` attempts
        with ``retry_backoff_s * 2**n`` seconds between each.
        """
        import asyncio

        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                return await self._client.request(method, path, **kwargs)
            except _RETRYABLE_EXCEPTIONS as e:
                last_exc = e
                if attempt == self.max_retries - 1:
                    break
                delay = self.retry_backoff_s * (2**attempt)
                log.debug(
                    "transient %s on %s %s; retrying in %.1fs",
                    type(e).__name__, method, path, delay,
                )
                await asyncio.sleep(delay)
        assert last_exc is not None  # retries exhausted
        raise last_exc

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
        resp = await self._request(
            "GET", "/tasks/next",
            params={"types": types_str, "worker_id": worker_id},
        )
        if resp.status_code == 204:
            return None
        if resp.status_code == 404:
            # Older backends without /tasks/next return 404; treat as no-task.
            log.warning("backend %s has no /tasks/next", self.base_url)
            return None
        resp.raise_for_status()

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
        resp.raise_for_status()
        return resp.json() or {}

    async def get_cancel_status(self, task_id: int) -> dict:
        """GET /tasks/{id}/cancel-status — cheap read-only cancel check."""
        resp = await self._request(
            "GET", f"/tasks/{task_id}/cancel-status",
        )
        resp.raise_for_status()
        return resp.json() or {}

    async def complete(self, task_id: int, result: dict) -> None:
        """PUT /tasks/{id}/complete — final success payload."""
        resp = await self._request(
            "PUT", f"/tasks/{task_id}/complete", json={"result": result},
        )
        resp.raise_for_status()

    async def fail(self, task_id: int, error: str) -> None:
        """PUT /tasks/{id}/fail — final failure payload."""
        resp = await self._request(
            "PUT", f"/tasks/{task_id}/fail", json={"error": error},
        )
        resp.raise_for_status()

    # ----- file transfer (remote mode workers) ----------------------

    async def download_file(
        self, task_id: int, filename: str, dest: Path,
    ) -> None:
        """GET /tasks/{id}/files/{filename} — streams to disk in 1 MB chunks."""
        async with self._client.stream(
            "GET", f"/tasks/{task_id}/files/{filename}",
        ) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                async for chunk in resp.aiter_bytes():
                    f.write(chunk)

    async def upload_file(
        self, task_id: int, filename: str, src: Path,
    ) -> None:
        """PUT /tasks/{id}/files/{filename} — multipart upload."""
        with open(src, "rb") as f:
            files = {"file": (filename, f)}
            resp = await self._request(
                "PUT", f"/tasks/{task_id}/files/{filename}", files=files,
            )
            resp.raise_for_status()
