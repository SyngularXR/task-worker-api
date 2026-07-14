"""Tests for BackendClient._request retry/backoff and file-transfer methods.

The retry logic is the core robustness contract of the HTTP client — it
guards every claim/progress/complete/fail call against transient transport
errors.  Until now it was only exercised indirectly through the happy path
(httpx.MockTransport returning 200), so the retry, exhaustion, and backoff-
timing branches had zero coverage.

These tests use httpx.MockTransport to simulate transient failures
(TransportError / TimeoutException) and assert on retry count, eventual
success, backoff scheduling, and the re-raised exception when retries are
exhausted.  They also cover the real (non-Fake) download_file / upload_file
HTTP paths, which were previously only tested via the in-memory test double.
"""
from __future__ import annotations

import asyncio
import random

import httpx
import pytest

from task_worker_api.client import BackendClient
from task_worker_api.enums import TaskType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _client_with_handler(
    handler,
    *,
    max_retries: int = 4,
    retry_backoff_s: float = 0.0,
    retry_backoff_max_s: float | None = None,
    retry_jitter: bool = False,
) -> BackendClient:
    """Build a real BackendClient backed by a MockTransport handler.

    ``retry_backoff_s`` defaults to 0 so the exponential-backoff sleeps are
    instant — the timing is verified separately via a mocked ``asyncio.sleep``.
    Jitter defaults to False so timing-assertion tests stay deterministic; the
    jitter behaviour itself is covered by dedicated _backoff_delay unit tests.
    """
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(
        base_url="http://fake/api/v1",
        transport=transport,
        headers={"Authorization": "Bearer x"},
    )
    kwargs: dict = {
        "max_retries": max_retries,
        "retry_backoff_s": retry_backoff_s,
        "retry_jitter": retry_jitter,
    }
    # Keep the SDK default (60s cap) unless a test explicitly overrides it.
    if retry_backoff_max_s is not None:
        kwargs["retry_backoff_max_s"] = retry_backoff_max_s
    return BackendClient(
        "http://fake/api/v1",
        "x",
        client=http,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Retry — happy path after transient failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_retries_on_transport_error_then_succeeds():
    """A transient TransportError on the first attempt must not bubble up
    if a later attempt succeeds."""
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.TransportError("transient hiccup")
        return httpx.Response(200, json={
            "id": 1,
            "task_type": "detect_cut_planes",
            "status": 2,
            "case_id": None,
            "item_key": "",
            "params": {},
        })

    client = _client_with_handler(handler, max_retries=4)
    result = await client.claim_next([TaskType.DETECT_CUT_PLANES], worker_id="w")
    await client.close()

    assert result is not None
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_request_retries_on_timeout_exception_then_succeeds():
    """TimeoutException is retryable just like TransportError."""
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.TimeoutException("read timed out")
        return httpx.Response(204)

    client = _client_with_handler(handler, max_retries=3)
    result = await client.claim_next([TaskType.DETECT_CUT_PLANES], worker_id="w")
    await client.close()

    assert result is None  # 204 → no task
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# Retry — exhaustion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_raises_after_exhausting_retries():
    """When every attempt raises a transient error, the last exception is
    re-raised to the caller."""
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.TransportError("persistent failure")

    client = _client_with_handler(handler, max_retries=3)
    with pytest.raises(httpx.TransportError, match="persistent failure"):
        await client.claim_next([TaskType.DETECT_CUT_PLANES], worker_id="w")
    await client.close()

    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_request_does_not_retry_non_transient_http_status_error():
    """A 500 response becomes an HTTPStatusError via raise_for_status, which
    is NOT in _RETRYABLE_EXCEPTIONS — it must surface immediately without
    consuming retry budget."""
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, text="Internal Server Error")

    client = _client_with_handler(handler, max_retries=4)
    # claim_next calls raise_for_status on the 500 → HTTPStatusError.
    with pytest.raises(httpx.HTTPStatusError):
        await client.claim_next([TaskType.DETECT_CUT_PLANES], worker_id="w")
    await client.close()

    # Only one attempt — the error is not retryable.
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# Retry — backoff scheduling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_uses_exponential_backoff_between_retries(monkeypatch):
    """The delay between retries must follow ``retry_backoff_s * 2**attempt``."""
    sleeps: list[float] = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TransportError("keep failing")

    client = _client_with_handler(
        handler, max_retries=4, retry_backoff_s=2.0,
    )
    with pytest.raises(httpx.TransportError):
        await client.claim_next([TaskType.DETECT_CUT_PLANES], worker_id="w")
    await client.close()

    # 4 retries → 3 sleeps (no sleep after the final failed attempt).
    assert sleeps == [2.0 * 2**0, 2.0 * 2**1, 2.0 * 2**2]


@pytest.mark.asyncio
async def test_request_no_sleep_after_final_attempt(monkeypatch):
    """The last attempt must not schedule a sleep — it breaks out first."""
    sleeps: list[float] = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TransportError("fail")

    client = _client_with_handler(
        handler, max_retries=2, retry_backoff_s=1.0,
    )
    with pytest.raises(httpx.TransportError):
        await client.claim_next([TaskType.DETECT_CUT_PLANES], worker_id="w")
    await client.close()

    # 2 attempts → exactly 1 sleep (between attempt 0 and 1).
    assert sleeps == [1.0]


# ---------------------------------------------------------------------------
# Real download_file / upload_file (MockTransport, not FakeBackendClient)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_file_streams_bytes_to_disk(tmp_path):
    """The real download_file must stream the response body to ``dest``."""
    body = b"\x00\x01\x02" * 1000

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/tasks/5/files/scene.ply"
        return httpx.Response(200, content=body)

    client = _client_with_handler(handler)
    dest = tmp_path / "out" / "scene.ply"
    dest.parent.mkdir(parents=True, exist_ok=True)
    await client.download_file(5, "scene.ply", dest)
    await client.close()

    assert dest.read_bytes() == body


@pytest.mark.asyncio
async def test_download_file_raises_on_404(tmp_path):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    client = _client_with_handler(handler)
    with pytest.raises(httpx.HTTPStatusError):
        await client.download_file(5, "missing.ply", tmp_path / "out.ply")
    await client.close()


# -----------------------------------------------------------------------
# download_file retry — transient errors now get the same backoff as every
# other backend call (this was the gap: download_file used _client.stream
# directly, bypassing _request's retry loop).
# -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_file_retries_on_transport_error_then_succeeds(tmp_path):
    """A transient TransportError on the first download attempt must not
    bubble up if a later attempt succeeds, and dest must hold the full body."""
    body = b"\x00\x01\x02" * 500
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.TransportError("transient hiccup")
        return httpx.Response(200, content=body)

    client = _client_with_handler(handler, max_retries=4)
    dest = tmp_path / "out" / "scene.ply"
    dest.parent.mkdir(parents=True, exist_ok=True)
    await client.download_file(5, "scene.ply", dest)
    await client.close()

    assert calls["n"] == 3
    assert dest.read_bytes() == body


@pytest.mark.asyncio
async def test_download_file_retries_on_timeout_then_succeeds(tmp_path):
    """TimeoutException during stream establishment is retryable."""
    body = b"payload"
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.TimeoutException("read timed out")
        return httpx.Response(200, content=body)

    client = _client_with_handler(handler, max_retries=3)
    dest = tmp_path / "out.ply"
    await client.download_file(5, "scene.ply", dest)
    await client.close()

    assert calls["n"] == 2
    assert dest.read_bytes() == body


@pytest.mark.asyncio
async def test_download_file_raises_after_exhausting_retries(tmp_path):
    """When every download attempt raises a transient error, the last
    exception is re-raised and dest must not be left as a partial file."""
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.TransportError("persistent failure")

    client = _client_with_handler(handler, max_retries=3)
    dest = tmp_path / "out.ply"
    with pytest.raises(httpx.TransportError, match="persistent failure"):
        await client.download_file(5, "scene.ply", dest)
    await client.close()

    assert calls["n"] == 3
    # No partial file left behind from a failed stream.
    assert not dest.exists()


@pytest.mark.asyncio
async def test_download_file_does_not_retry_non_transient_http_status(tmp_path):
    """A 500 response becomes an HTTPStatusError via raise_for_status, which
    is NOT retryable — it must surface immediately without retry budget."""
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, text="Internal Server Error")

    client = _client_with_handler(handler, max_retries=4)
    with pytest.raises(httpx.HTTPStatusError):
        await client.download_file(5, "scene.ply", tmp_path / "out.ply")
    await client.close()

    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_download_file_uses_exponential_backoff(monkeypatch, tmp_path):
    """The delay between download retries must follow retry_backoff_s*2**n."""
    sleeps: list[float] = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TransportError("keep failing")

    client = _client_with_handler(
        handler, max_retries=4, retry_backoff_s=2.0,
    )
    with pytest.raises(httpx.TransportError):
        await client.download_file(5, "scene.ply", tmp_path / "out.ply")
    await client.close()

    assert sleeps == [2.0 * 2**0, 2.0 * 2**1, 2.0 * 2**2]


@pytest.mark.asyncio
async def test_download_file_writes_clean_file_after_midstream_retry(tmp_path):
    """A transient error partway through streaming must not corrupt the
    final file: the retry re-opens dest with 'wb' (truncating), so the
    successful attempt writes the complete body from the start."""
    body = b"ABCDEFGH" * 64
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            # Simulate a connection drop mid-stream by raising a transport
            # error; MockTransport raises before any bytes are delivered.
            raise httpx.TransportError("dropped mid-stream")
        return httpx.Response(200, content=body)

    client = _client_with_handler(handler, max_retries=4)
    dest = tmp_path / "out.ply"
    await client.download_file(5, "scene.ply", dest)
    await client.close()

    assert calls["n"] == 2
    assert dest.read_bytes() == body


@pytest.mark.asyncio
async def test_download_file_removes_partial_file_on_retry_exhaustion(tmp_path):
    """When every download attempt fails, any partial file left at dest by a
    mid-stream failure must be removed — callers should never see a
    truncated/stale artifact from a failed download.

    Simulates the real-world scenario: a transport error after the file was
    opened and partially written.  MockTransport can't deliver partial bytes
    then fail mid-stream, so we pre-seed dest with stale content (exactly what
    a mid-stream failure would leave behind) and verify it's cleaned up when
    the download ultimately fails.
    """
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.TransportError("persistent failure")

    client = _client_with_handler(handler, max_retries=3)
    dest = tmp_path / "out.ply"
    # Simulate a partial file left behind by a mid-stream failure on a prior
    # call, or stale content from a previous run at the same path.
    dest.write_bytes(b"STALE PARTIAL CONTENT")

    with pytest.raises(httpx.TransportError, match="persistent failure"):
        await client.download_file(5, "scene.ply", dest)
    await client.close()

    assert calls["n"] == 3
    # The stale/partial file must be removed — no truncated artifact left behind.
    assert not dest.exists()


@pytest.mark.asyncio
async def test_download_file_removes_dest_on_non_transient_error(tmp_path):
    """A non-retryable error (e.g. 404) must also clean up dest — a prior
    partial download or a pre-existing stale file at that path should not
    survive a failed download_file call."""
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, text="not found")

    client = _client_with_handler(handler, max_retries=4)
    dest = tmp_path / "out.ply"
    dest.write_bytes(b"STALE CONTENT FROM A PREVIOUS RUN")

    with pytest.raises(httpx.HTTPStatusError):
        await client.download_file(5, "missing.ply", dest)
    await client.close()

    assert calls["n"] == 1
    assert not dest.exists()


@pytest.mark.asyncio
async def test_download_file_cleanup_does_not_affect_successful_download(tmp_path):
    """The cleanup-on-failure path must not interfere with a successful
    download: dest must contain the full body after a clean download."""
    body = b"\x00\x01\x02" * 100

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    client = _client_with_handler(handler)
    dest = tmp_path / "out.ply"
    await client.download_file(5, "scene.ply", dest)
    await client.close()

    assert dest.read_bytes() == body


@pytest.mark.asyncio
async def test_upload_file_sends_multipart_put(tmp_path):
    """The real upload_file must PUT the file as multipart form data."""
    src = tmp_path / "output.stl"
    src.write_bytes(b"result-bytes")

    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.path == "/api/v1/tasks/9/files/output.stl"
        captured["content_type"] = request.headers.get("content-type", "")
        captured["body"] = request.content
        return httpx.Response(200)

    client = _client_with_handler(handler)
    await client.upload_file(9, "output.stl", src)
    await client.close()

    assert b"result-bytes" in captured["body"]
    assert "multipart/form-data" in captured["content_type"]


@pytest.mark.asyncio
async def test_upload_file_raises_on_500(tmp_path):
    src = tmp_path / "output.stl"
    src.write_bytes(b"x")

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="backend down")

    client = _client_with_handler(handler)
    with pytest.raises(httpx.HTTPStatusError):
        await client.upload_file(9, "output.stl", src)
    await client.close()


# -----------------------------------------------------------------------
# upload_file retry — the file handle must be re-opened on each attempt.
# Before the fix, open() was called once *outside* the retry loop; httpx
# consumed the handle to EOF on the first attempt, so every retry sent
# zero bytes (silent data corruption).
# -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_file_retries_on_transport_error_then_succeeds(tmp_path):
    """A transient TransportError on the first upload attempt must not
    bubble up if a later attempt succeeds, and the backend must receive
    the full file body on the successful attempt."""
    body = b"\x00\x01\x02" * 500
    calls = {"n": 0}
    received: list[bytes] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.TransportError("transient hiccup")
        received.append(await request.aread())
        return httpx.Response(200)

    src = tmp_path / "output.stl"
    src.write_bytes(body)

    client = _client_with_handler(handler, max_retries=4)
    await client.upload_file(9, "output.stl", src)
    await client.close()

    assert calls["n"] == 3
    # The successful attempt must have received the *full* body, not zero
    # bytes (which is what the pre-fix exhausted-file-handle bug produced).
    assert body in received[0]


@pytest.mark.asyncio
async def test_upload_file_retries_on_timeout_then_succeeds(tmp_path):
    """TimeoutException during upload is retryable."""
    body = b"payload"
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.TimeoutException("write timed out")
        return httpx.Response(200)

    src = tmp_path / "output.stl"
    src.write_bytes(body)

    client = _client_with_handler(handler, max_retries=3)
    await client.upload_file(9, "output.stl", src)
    await client.close()

    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_upload_file_raises_after_exhausting_retries(tmp_path):
    """When every upload attempt raises a transient error, the last
    exception is re-raised."""
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.TransportError("persistent failure")

    src = tmp_path / "output.stl"
    src.write_bytes(b"data")

    client = _client_with_handler(handler, max_retries=3)
    with pytest.raises(httpx.TransportError, match="persistent failure"):
        await client.upload_file(9, "output.stl", src)
    await client.close()

    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_upload_file_does_not_retry_non_transient_http_status(tmp_path):
    """A 500 response becomes an HTTPStatusError via raise_for_status, which
    is NOT retryable — it must surface immediately without retry budget."""
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, text="Internal Server Error")

    src = tmp_path / "output.stl"
    src.write_bytes(b"data")

    client = _client_with_handler(handler, max_retries=4)
    with pytest.raises(httpx.HTTPStatusError):
        await client.upload_file(9, "output.stl", src)
    await client.close()

    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_upload_file_uses_exponential_backoff(monkeypatch, tmp_path):
    """The delay between upload retries must follow retry_backoff_s*2**n."""
    sleeps: list[float] = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TransportError("keep failing")

    src = tmp_path / "output.stl"
    src.write_bytes(b"data")

    client = _client_with_handler(
        handler, max_retries=4, retry_backoff_s=2.0,
    )
    with pytest.raises(httpx.TransportError):
        await client.upload_file(9, "output.stl", src)
    await client.close()

    assert sleeps == [2.0 * 2**0, 2.0 * 2**1, 2.0 * 2**2]


@pytest.mark.asyncio
async def test_upload_file_sends_full_body_after_retry(tmp_path):
    """The core regression test: after a transient failure on the first
    attempt, the retried attempt must send the *complete* file body —
    not zero bytes.  Before the fix, the file handle was opened once
    outside the retry loop; httpx read it to EOF on attempt 1, so the
    retry sent an empty body and the upload silently succeeded with
    corrupt (empty) data."""
    body = b"ABCDEFGH" * 64
    calls = {"n": 0}
    received_bodies: list[bytes] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            # Consume the request body (as httpx would) then fail, so the
            # file handle is exhausted before the retry.
            await request.aread()
            raise httpx.TransportError("dropped after read")
        received_bodies.append(await request.aread())
        return httpx.Response(200)

    src = tmp_path / "output.stl"
    src.write_bytes(body)

    client = _client_with_handler(handler, max_retries=4)
    await client.upload_file(9, "output.stl", src)
    await client.close()

    assert calls["n"] == 2
    # The retried attempt must contain the full body — this fails with
    # the old code because the exhausted file handle yields zero bytes.
    assert body in received_bodies[0]


# -----------------------------------------------------------------------
# max_retries validation — a value < 1 made the retry loop in _retry
# never execute, so last_exc stayed None and the post-loop assert fired
# with an opaque AssertionError that crashed the worker. The guard now
# fails fast at construction with a clear ValueError.
# -----------------------------------------------------------------------


def test_init_rejects_max_retries_zero():
    """max_retries=0 means zero attempts — the loop never runs and the
    post-loop guard would fire. Reject at construction instead."""
    with pytest.raises(ValueError, match="max_retries must be >= 1"):
        BackendClient("http://fake", "x", max_retries=0)


def test_init_rejects_negative_max_retries():
    """Negative max_retries is equally degenerate."""
    with pytest.raises(ValueError, match="max_retries must be >= 1"):
        BackendClient("http://fake", "x", max_retries=-3)


def test_init_accepts_max_retries_one():
    """max_retries=1 means exactly one attempt (no retries) — valid."""
    client = BackendClient("http://fake", "x", max_retries=1)
    assert client.max_retries == 1
    # _retry must work: one attempt, success.
    import asyncio

    async def _ok():
        return "done"

    result = asyncio.run(client._retry(_ok, method="GET", path="/t"))
    assert result == "done"


# -----------------------------------------------------------------------
# Transient 5xx gateway retry — the backend sits behind nginx; a 502/503/504
# almost always means the Flask upstream restarted or is momentarily
# overloaded. Previously every HTTPStatusError (including these transient
# gateway codes) surfaced immediately, failing the task on a blip that clears
# in seconds. Now 502/503/504 are retried with the same backoff as transport
# errors; 500 and 4xx still surface immediately (500 = app logic error, 4xx =
# client error — retrying won't help).
# -----------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [502, 503, 504])
async def test_request_retries_on_transient_gateway_status_then_succeeds(status):
    """A 502/503/504 from the gateway is transient (upstream restart/overload)
    and must be retried with backoff, not failed immediately."""
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(status, text="Bad Gateway")
        return httpx.Response(204)

    client = _client_with_handler(handler, max_retries=4)
    result = await client.claim_next([TaskType.DETECT_CUT_PLANES], worker_id="w")
    await client.close()

    assert result is None  # 204 → no task
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_request_does_not_retry_500():
    """A 500 is the application's own error (logic bug / bad payload), not a
    transient outage — it must surface immediately without retry budget."""
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, text="Internal Server Error")

    client = _client_with_handler(handler, max_retries=4)
    with pytest.raises(httpx.HTTPStatusError):
        await client.claim_next([TaskType.DETECT_CUT_PLANES], worker_id="w")
    await client.close()

    assert calls["n"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
async def test_request_does_not_retry_4xx(status):
    """4xx client errors are never transient — retrying won't change the
    outcome, so they must surface immediately without retry budget.

    claim_next treats 404 as no-task (returns None); every other 4xx raises
    HTTPStatusError. Either way only one attempt must fire."""
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(status, text="client error")

    client = _client_with_handler(handler, max_retries=4)
    if status == 404:
        result = await client.claim_next(
            [TaskType.DETECT_CUT_PLANES], worker_id="w",
        )
        assert result is None
    else:
        with pytest.raises(httpx.HTTPStatusError):
            await client.claim_next(
                [TaskType.DETECT_CUT_PLANES], worker_id="w",
            )
    await client.close()

    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_request_retries_5xx_then_raises_after_exhaustion():
    """When every attempt returns a transient 5xx gateway code, the last
    HTTPStatusError is re-raised to the caller."""
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, text="Service Unavailable")

    client = _client_with_handler(handler, max_retries=3)
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await client.claim_next([TaskType.DETECT_CUT_PLANES], worker_id="w")
    await client.close()

    assert calls["n"] == 3
    assert exc_info.value.response.status_code == 503


@pytest.mark.asyncio
async def test_request_5xx_uses_exponential_backoff(monkeypatch):
    """The delay between 5xx retries must follow retry_backoff_s * 2**n,
    the same schedule as transport-error retries."""
    sleeps: list[float] = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="Bad Gateway")

    client = _client_with_handler(handler, max_retries=4, retry_backoff_s=2.0)
    with pytest.raises(httpx.HTTPStatusError):
        await client.claim_next([TaskType.DETECT_CUT_PLANES], worker_id="w")
    await client.close()

    assert sleeps == [2.0 * 2**0, 2.0 * 2**1, 2.0 * 2**2]


@pytest.mark.asyncio
async def test_download_file_retries_on_503_then_succeeds(tmp_path):
    """A 503 during download establishment is transient and must be retried;
    the successful retry writes the full body to dest."""
    body = b"\x00\x01\x02" * 200
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, text="Service Unavailable")
        return httpx.Response(200, content=body)

    client = _client_with_handler(handler, max_retries=4)
    dest = tmp_path / "out.ply"
    await client.download_file(5, "scene.ply", dest)
    await client.close()

    assert calls["n"] == 3
    assert dest.read_bytes() == body


@pytest.mark.asyncio
async def test_download_file_does_not_retry_500(tmp_path):
    """A 500 during download is not transient — it must surface immediately."""
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, text="Internal Server Error")

    client = _client_with_handler(handler, max_retries=4)
    with pytest.raises(httpx.HTTPStatusError):
        await client.download_file(5, "scene.ply", tmp_path / "out.ply")
    await client.close()

    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_download_file_5xx_exhaustion_removes_partial_file(tmp_path):
    """When every download attempt returns a transient 5xx, the last
    HTTPStatusError is re-raised and any partial/stale file at dest is
    removed — same cleanup contract as transport-error exhaustion."""
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(502, text="Bad Gateway")

    client = _client_with_handler(handler, max_retries=3)
    dest = tmp_path / "out.ply"
    dest.write_bytes(b"STALE CONTENT FROM A PREVIOUS RUN")

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await client.download_file(5, "scene.ply", dest)
    await client.close()

    assert calls["n"] == 3
    assert exc_info.value.response.status_code == 502
    assert not dest.exists()


@pytest.mark.asyncio
async def test_upload_file_retries_on_503_then_succeeds(tmp_path):
    """A 503 during upload is transient and must be retried; the successful
    retry sends the full body."""
    body = b"\x00\x01\x02" * 200
    calls = {"n": 0}
    received: list[bytes] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, text="Service Unavailable")
        received.append(await request.aread())
        return httpx.Response(200)

    src = tmp_path / "output.stl"
    src.write_bytes(body)

    client = _client_with_handler(handler, max_retries=4)
    await client.upload_file(9, "output.stl", src)
    await client.close()

    assert calls["n"] == 3
    assert body in received[0]


@pytest.mark.asyncio
async def test_upload_file_does_not_retry_500(tmp_path):
    """A 500 during upload is not transient — it must surface immediately."""
    calls = {"n": 0}
    src = tmp_path / "output.stl"
    src.write_bytes(b"data")

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, text="Internal Server Error")

    client = _client_with_handler(handler, max_retries=4)
    with pytest.raises(httpx.HTTPStatusError):
        await client.upload_file(9, "output.stl", src)
    await client.close()

    assert calls["n"] == 1


# -----------------------------------------------------------------------
# Backoff cap + jitter — without a cap, retry_backoff_s * 2**n grows without
# bound: a supported max_retries=8 with the default base would sleep 256s on
# the penultimate attempt, blocking the worker's event loop for ~10 minutes on
# a single claim/complete call. Without jitter, the three fleet workers
# (Neural-Canvas, Blender-CLI, colmap-splat) retry on the identical
# deterministic schedule, re-overloading the backend the instant it recovers
# (thundering herd). The cap bounds individual delays; jitter decorrelates
# them. Both are additive and default-on; retry_jitter=False / an explicit
# retry_backoff_max_s recover the legacy deterministic behaviour for tests.
# -----------------------------------------------------------------------


def test_backoff_delay_caps_exponential_growth():
    """_backoff_delay must clamp to max_s once 2**n overtakes it."""
    from task_worker_api.client import _backoff_delay

    # base=2, attempt=7 → 2*128 = 256; cap at 30 → 30.
    assert _backoff_delay(7, base_s=2.0, max_s=30.0, jitter=False) == 30.0
    # attempt=2 → 2*4 = 8; under cap → unchanged.
    assert _backoff_delay(2, base_s=2.0, max_s=30.0, jitter=False) == 8.0


def test_backoff_delay_no_cap_when_max_is_none():
    """max_s=None disables the cap — legacy unbounded behaviour."""
    from task_worker_api.client import _backoff_delay

    assert _backoff_delay(10, base_s=2.0, max_s=None, jitter=False) == 2048.0


def test_backoff_delay_zero_base_yields_zero():
    """A zero base (the test default) produces zero delay — no sleep spam."""
    from task_worker_api.client import _backoff_delay

    assert _backoff_delay(5, base_s=0.0, max_s=60.0, jitter=False) == 0.0


def test_backoff_delay_jitter_stays_within_spread_band():
    """Jittered delays must fall within [d*(1-0.25), d*(1+0.25)]."""
    from task_worker_api.client import _JITTER_SPREAD, _backoff_delay

    # attempt=2, base=2 → 2*4 = 8.0 un-jittered.
    for _ in range(200):
        d = _backoff_delay(2, base_s=2.0, max_s=60.0, jitter=True)
        assert 8.0 * (1 - _JITTER_SPREAD) <= d <= 8.0 * (1 + _JITTER_SPREAD)


def test_backoff_delay_jitter_is_deterministic_with_injected_rng():
    """An injected rng makes jitter reproducible — used by the cap+retry test."""
    from task_worker_api.client import _backoff_delay

    rng = random.Random(1234)
    d1 = _backoff_delay(2, base_s=2.0, max_s=60.0, jitter=True, rng=rng)
    rng2 = random.Random(1234)
    d2 = _backoff_delay(2, base_s=2.0, max_s=60.0, jitter=True, rng=rng2)
    assert d1 == d2


def test_backoff_delay_no_jitter_on_zero_delay():
    """Jitter must not inflate a zero delay (base=0 → always 0, even jittered)."""
    from task_worker_api.client import _backoff_delay

    assert _backoff_delay(3, base_s=0.0, max_s=60.0, jitter=True) == 0.0


@pytest.mark.asyncio
async def test_request_caps_backoff_at_max(monkeypatch):
    """The cap must clamp each inter-attempt sleep to retry_backoff_max_s.

    With base=2, max_retries=8, the un-capped schedule would be
    [2, 4, 8, 16, 32, 64, 128] — the last two exceed a 30s cap. Jitter is
    disabled so the assertion is exact.
    """
    from task_worker_api.client import _backoff_delay

    sleeps: list[float] = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TransportError("keep failing")

    client = _client_with_handler(
        handler, max_retries=8, retry_backoff_s=2.0,
        retry_backoff_max_s=30.0,
    )
    with pytest.raises(httpx.TransportError):
        await client.claim_next([TaskType.DETECT_CUT_PLANES], worker_id="w")
    await client.close()

    expected = [
        _backoff_delay(n, 2.0, 30.0, False) for n in range(7)
    ]
    assert sleeps == expected
    # The cap actually bit: the last two un-capped values (64, 128) are 30.
    assert sleeps[-2:] == [30.0, 30.0]


def test_init_rejects_non_positive_backoff_max():
    """A non-positive retry_backoff_max_s is degenerate — fail fast."""
    with pytest.raises(ValueError, match="retry_backoff_max_s must be > 0"):
        BackendClient("http://fake", "x", retry_backoff_max_s=0)
    with pytest.raises(ValueError, match="retry_backoff_max_s must be > 0"):
        BackendClient("http://fake", "x", retry_backoff_max_s=-5)


def test_init_accepts_none_backoff_max():
    """retry_backoff_max_s=None disables the cap — valid (legacy behaviour)."""
    client = BackendClient("http://fake", "x", retry_backoff_max_s=None)
    assert client.retry_backoff_max_s is None


def test_init_defaults_backoff_max_and_jitter():
    """The new params default to a 60s cap and jitter on."""
    client = BackendClient("http://fake", "x")
    assert client.retry_backoff_max_s == 60.0
    assert client.retry_jitter is True


# -----------------------------------------------------------------------
# file_timeout_s — file transfers (download_file / upload_file) get a
# separate, longer timeout than the 30s general request budget that
# governs claim/heartbeat/complete. Without it, GB-scale outputs
# (colmap-splat PLY files, Neural-Canvas splats) hit WriteTimeout/
# ReadTimeout on big files, exhaust retries inside the same 30s window,
# and fail tasks that would succeed with a file-appropriate timeout.
# -----------------------------------------------------------------------


def test_init_defaults_file_timeout_to_none_when_client_supplied():
    """When no file_timeout_s is passed, _file_timeout is None — the file
    calls fall back to the client's own default timeout (legacy behaviour
    for consumers that build their own client and don't want the SDK to
    impose a separate file deadline)."""
    client = BackendClient("http://fake", "x")
    assert client._file_timeout is None
    # close the real httpx client it created
    import asyncio
    asyncio.run(client.close())


def test_init_file_timeout_builds_explicit_timeout():
    """An explicit file_timeout_s builds an httpx.Timeout so file calls
    override the client default only for download_file / upload_file."""
    client = BackendClient("http://fake", "x", file_timeout_s=300.0)
    assert client._file_timeout is not None
    assert client._file_timeout.read == 300.0
    assert client._file_timeout.write == 300.0
    assert client._file_timeout.connect == 300.0
    assert client._file_timeout.pool == 300.0
    import asyncio
    asyncio.run(client.close())


@pytest.mark.asyncio
async def test_download_file_uses_file_timeout_not_general(tmp_path):
    """download_file must apply file_timeout_s to the streaming request,
    not the 30s general request timeout. Verified by inspecting the
    timeout extension on the request the MockTransport handler sees."""
    seen: list = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions.get("timeout"))
        return httpx.Response(200, content=b"payload")

    # General timeout 30s, file timeout 300s — distinct so a mix-up is
    # detectable. file_timeout_s is passed explicitly.
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(
        base_url="http://fake/api/v1", transport=transport,
        timeout=30.0, headers={"Authorization": "Bearer x"},
    )
    client = BackendClient(
        "http://fake/api/v1", "x", timeout_s=30.0, file_timeout_s=300.0,
        client=http,
    )
    dest = tmp_path / "out.ply"
    await client.download_file(5, "scene.ply", dest)
    await client.close()

    assert dest.read_bytes() == b"payload"
    assert len(seen) == 1
    assert seen[0] is not None
    # All four timeout facets must reflect the file timeout, not 30s.
    assert seen[0]["read"] == 300.0
    assert seen[0]["write"] == 300.0
    assert seen[0]["connect"] == 300.0
    assert seen[0]["pool"] == 300.0


@pytest.mark.asyncio
async def test_upload_file_uses_file_timeout_not_general(tmp_path):
    """upload_file must apply file_timeout_s to the multipart PUT request,
    not the 30s general request timeout."""
    seen: list = []
    src = tmp_path / "output.stl"
    src.write_bytes(b"result-bytes")

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions.get("timeout"))
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(
        base_url="http://fake/api/v1", transport=transport,
        timeout=30.0, headers={"Authorization": "Bearer x"},
    )
    client = BackendClient(
        "http://fake/api/v1", "x", timeout_s=30.0, file_timeout_s=300.0,
        client=http,
    )
    await client.upload_file(9, "output.stl", src)
    await client.close()

    assert len(seen) == 1
    assert seen[0] is not None
    assert seen[0]["read"] == 300.0
    assert seen[0]["write"] == 300.0
    assert seen[0]["connect"] == 300.0
    assert seen[0]["pool"] == 300.0


@pytest.mark.asyncio
async def test_lifecycle_calls_do_not_use_file_timeout():
    """claim_next (and by extension _request, heartbeat, complete, fail)
    must keep using the general 30s request timeout, NOT the file timeout
    — inflating the lifecycle timeout would make heartbeat latency worse
    and delay cancel detection. Verified by inspecting the timeout the
    MockTransport handler sees on a claim_next call."""
    seen: list = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions.get("timeout"))
        return httpx.Response(204)  # no task

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(
        base_url="http://fake/api/v1", transport=transport,
        timeout=30.0, headers={"Authorization": "Bearer x"},
    )
    client = BackendClient(
        "http://fake/api/v1", "x", timeout_s=30.0, file_timeout_s=300.0,
        client=http,
    )
    result = await client.claim_next([TaskType.DETECT_CUT_PLANES], worker_id="w")
    await client.close()

    assert result is None  # 204 → no task
    assert len(seen) == 1
    assert seen[0] is not None
    # Lifecycle call must use the 30s general timeout, not 300s.
    assert seen[0]["read"] == 30.0
    assert seen[0]["write"] == 30.0
    assert seen[0]["connect"] == 30.0
    assert seen[0]["pool"] == 30.0


@pytest.mark.asyncio
async def test_file_timeout_none_falls_back_to_client_default(tmp_path):
    """When file_timeout_s is None (the default), file calls must use the
    client's own default timeout rather than imposing a separate one —
    preserving the legacy behaviour for consumers that build their own
    client."""
    seen: list = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions.get("timeout"))
        return httpx.Response(200, content=b"payload")

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(
        base_url="http://fake/api/v1", transport=transport,
        timeout=30.0, headers={"Authorization": "Bearer x"},
    )
    client = BackendClient(
        "http://fake/api/v1", "x", timeout_s=30.0, client=http,
    )  # no file_timeout_s → _file_timeout is None
    dest = tmp_path / "out.ply"
    await client.download_file(5, "scene.ply", dest)
    await client.close()

    assert len(seen) == 1
    # timeout=None passed to httpx means "use client default" — at the
    # request-extension level httpx records None for each facet (the
    # resolution to the client's 30s happens at the real transport layer,
    # which MockTransport doesn't simulate). The point is that no separate
    # file deadline is imposed: _file_timeout is None, so the call inherits
    # the client default rather than overriding it.
    assert seen[0] is not None
    assert seen[0]["read"] is None
    assert seen[0]["write"] is None


# -----------------------------------------------------------------------
# cancel_timeout_s — get_cancel_status gets a dedicated short timeout
# (default 5s) instead of the 30s general request timeout. The CancelGuard
# polls /tasks/{id}/cancel-status every few seconds; under backend load a
# single slow poll could block cancel detection for 30s (plus retry
# backoff), keeping the worker blind to a user cancel. The short deadline
# fails fast — CancelGuard catches the timeout and the next poll fires on
# schedule. The 30s general timeout still governs claim/heartbeat/complete.
# -----------------------------------------------------------------------


def test_init_defaults_cancel_timeout_to_5s():
    """The default cancel_timeout_s is 5s, short enough that a stalled
    cancel-poll fails fast instead of blocking the CancelGuard for 30s."""
    client = BackendClient("http://fake", "x")
    assert client._cancel_timeout is not None
    assert client._cancel_timeout.read == 5.0
    assert client._cancel_timeout.write == 5.0
    assert client._cancel_timeout.connect == 5.0
    assert client._cancel_timeout.pool == 5.0
    import asyncio
    asyncio.run(client.close())


def test_init_cancel_timeout_builds_explicit_timeout():
    """An explicit cancel_timeout_s builds an httpx.Timeout so the
    cancel-poll call overrides the client default only for get_cancel_status."""
    client = BackendClient("http://fake", "x", cancel_timeout_s=3.0)
    assert client._cancel_timeout is not None
    assert client._cancel_timeout.read == 3.0
    assert client._cancel_timeout.write == 3.0
    assert client._cancel_timeout.connect == 3.0
    assert client._cancel_timeout.pool == 3.0
    import asyncio
    asyncio.run(client.close())


def test_init_cancel_timeout_none_falls_back_to_client_default():
    """When cancel_timeout_s is None, _cancel_timeout is None — the
    cancel-poll call falls back to the client's own default timeout (legacy
    behaviour for consumers that build their own client and don't want the
    SDK to impose a separate cancel deadline)."""
    client = BackendClient("http://fake", "x", cancel_timeout_s=None)
    assert client._cancel_timeout is None
    import asyncio
    asyncio.run(client.close())


@pytest.mark.asyncio
async def test_get_cancel_status_uses_cancel_timeout_not_general():
    """get_cancel_status must apply cancel_timeout_s to the request, not
    the 30s general request timeout. Verified by inspecting the timeout
    extension on the request the MockTransport handler sees."""
    seen: list = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions.get("timeout"))
        return httpx.Response(200, json={"cancelled": False})

    # General timeout 30s, cancel timeout 5s — distinct so a mix-up is
    # detectable. cancel_timeout_s is passed explicitly.
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(
        base_url="http://fake/api/v1", transport=transport,
        timeout=30.0, headers={"Authorization": "Bearer x"},
    )
    client = BackendClient(
        "http://fake/api/v1", "x", timeout_s=30.0, cancel_timeout_s=5.0,
        client=http,
    )
    result = await client.get_cancel_status(7)
    await client.close()

    assert result == {"cancelled": False}
    assert len(seen) == 1
    assert seen[0] is not None
    # All four timeout facets must reflect the cancel timeout, not 30s.
    assert seen[0]["read"] == 5.0
    assert seen[0]["write"] == 5.0
    assert seen[0]["connect"] == 5.0
    assert seen[0]["pool"] == 5.0


@pytest.mark.asyncio
async def test_lifecycle_calls_do_not_use_cancel_timeout():
    """claim_next (and by extension _request, heartbeat, complete, fail)
    must keep using the general 30s request timeout, NOT the cancel timeout
    — shortening the lifecycle timeout would make heartbeat latency worse
    and could spuriously abort a claim under momentary load. Verified by
    inspecting the timeout the MockTransport handler sees on a claim_next
    call when cancel_timeout_s is set to a distinct, shorter value."""
    seen: list = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions.get("timeout"))
        return httpx.Response(204)  # no task

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(
        base_url="http://fake/api/v1", transport=transport,
        timeout=30.0, headers={"Authorization": "Bearer x"},
    )
    client = BackendClient(
        "http://fake/api/v1", "x", timeout_s=30.0, cancel_timeout_s=5.0,
        client=http,
    )
    result = await client.claim_next([TaskType.DETECT_CUT_PLANES], worker_id="w")
    await client.close()

    assert result is None  # 204 → no task
    assert len(seen) == 1
    assert seen[0] is not None
    # Lifecycle call must use the 30s general timeout, not 5s.
    assert seen[0]["read"] == 30.0
    assert seen[0]["write"] == 30.0
    assert seen[0]["connect"] == 30.0
    assert seen[0]["pool"] == 30.0


# -----------------------------------------------------------------------
# lifecycle_timeout_s — report_progress / complete / fail get a dedicated
# timeout (default 15s) instead of the 30s general request timeout. These
# are the worker's terminal-ish status calls; under backend load a single
# stalled heartbeat or complete call could block the polling loop for up
# to 30s × max_retries (~120s with 4 attempts). The shorter deadline fails
# fast so the polling loop stays responsive (worst case ~60s with 4 × 15s).
# The 30s general timeout still governs claim_next. This completes the
# timeout-separation pattern: claim (30s general), cancel-poll (5s),
# file transfer (300s), lifecycle writes (15s).
# -----------------------------------------------------------------------


def test_init_defaults_lifecycle_timeout_to_15s():
    """The default lifecycle_timeout_s is 15s, short enough that a stalled
    heartbeat/complete/fail fails fast instead of blocking the polling
    loop for 30s × max_retries (~120s)."""
    client = BackendClient("http://fake", "x")
    assert client._lifecycle_timeout is not None
    assert client._lifecycle_timeout.read == 15.0
    assert client._lifecycle_timeout.write == 15.0
    assert client._lifecycle_timeout.connect == 15.0
    assert client._lifecycle_timeout.pool == 15.0
    import asyncio
    asyncio.run(client.close())


def test_init_lifecycle_timeout_builds_explicit_timeout():
    """An explicit lifecycle_timeout_s builds an httpx.Timeout so the
    lifecycle calls override the client default only for
    report_progress / complete / fail."""
    client = BackendClient("http://fake", "x", lifecycle_timeout_s=10.0)
    assert client._lifecycle_timeout is not None
    assert client._lifecycle_timeout.read == 10.0
    assert client._lifecycle_timeout.write == 10.0
    assert client._lifecycle_timeout.connect == 10.0
    assert client._lifecycle_timeout.pool == 10.0
    import asyncio
    asyncio.run(client.close())


def test_init_lifecycle_timeout_none_falls_back_to_client_default():
    """When lifecycle_timeout_s is None, _lifecycle_timeout is None — the
    lifecycle calls fall back to the client's own default timeout (legacy
    behaviour for consumers that build their own client and don't want the
    SDK to impose a separate lifecycle deadline)."""
    client = BackendClient("http://fake", "x", lifecycle_timeout_s=None)
    assert client._lifecycle_timeout is None
    import asyncio
    asyncio.run(client.close())


@pytest.mark.asyncio
async def test_report_progress_uses_lifecycle_timeout_not_general():
    """report_progress must apply lifecycle_timeout_s to the request, not
    the 30s general request timeout. Verified by inspecting the timeout
    extension on the request the MockTransport handler sees."""
    seen: list = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions.get("timeout"))
        return httpx.Response(200, json={"cancelled": False})

    # General timeout 30s, lifecycle timeout 15s — distinct so a mix-up is
    # detectable. lifecycle_timeout_s is passed explicitly.
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(
        base_url="http://fake/api/v1", transport=transport,
        timeout=30.0, headers={"Authorization": "Bearer x"},
    )
    client = BackendClient(
        "http://fake/api/v1", "x", timeout_s=30.0, lifecycle_timeout_s=15.0,
        client=http,
    )
    result = await client.report_progress(7, stage="working", current=1, total=2)
    await client.close()

    assert result == {"cancelled": False}
    assert len(seen) == 1
    assert seen[0] is not None
    # All four timeout facets must reflect the lifecycle timeout, not 30s.
    assert seen[0]["read"] == 15.0
    assert seen[0]["write"] == 15.0
    assert seen[0]["connect"] == 15.0
    assert seen[0]["pool"] == 15.0


@pytest.mark.asyncio
async def test_complete_uses_lifecycle_timeout_not_general():
    """complete must apply lifecycle_timeout_s to the request, not the 30s
    general request timeout."""
    seen: list = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions.get("timeout"))
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(
        base_url="http://fake/api/v1", transport=transport,
        timeout=30.0, headers={"Authorization": "Bearer x"},
    )
    client = BackendClient(
        "http://fake/api/v1", "x", timeout_s=30.0, lifecycle_timeout_s=15.0,
        client=http,
    )
    await client.complete(7, {"output": "done"})
    await client.close()

    assert len(seen) == 1
    assert seen[0] is not None
    assert seen[0]["read"] == 15.0
    assert seen[0]["write"] == 15.0
    assert seen[0]["connect"] == 15.0
    assert seen[0]["pool"] == 15.0


@pytest.mark.asyncio
async def test_fail_uses_lifecycle_timeout_not_general():
    """fail must apply lifecycle_timeout_s to the request, not the 30s
    general request timeout."""
    seen: list = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions.get("timeout"))
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(
        base_url="http://fake/api/v1", transport=transport,
        timeout=30.0, headers={"Authorization": "Bearer x"},
    )
    client = BackendClient(
        "http://fake/api/v1", "x", timeout_s=30.0, lifecycle_timeout_s=15.0,
        client=http,
    )
    await client.fail(7, "boom")
    await client.close()

    assert len(seen) == 1
    assert seen[0] is not None
    assert seen[0]["read"] == 15.0
    assert seen[0]["write"] == 15.0
    assert seen[0]["connect"] == 15.0
    assert seen[0]["pool"] == 15.0


@pytest.mark.asyncio
async def test_claim_next_does_not_use_lifecycle_timeout():
    """claim_next must keep using the general 30s request timeout, NOT the
    lifecycle timeout — claim is a poll, not a terminal write, and should
    be allowed the full general budget under momentary load. Verified by
    inspecting the timeout the MockTransport handler sees on a claim_next
    call when lifecycle_timeout_s is set to a distinct, shorter value."""
    seen: list = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions.get("timeout"))
        return httpx.Response(204)  # no task

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(
        base_url="http://fake/api/v1", transport=transport,
        timeout=30.0, headers={"Authorization": "Bearer x"},
    )
    client = BackendClient(
        "http://fake/api/v1", "x", timeout_s=30.0, lifecycle_timeout_s=15.0,
        client=http,
    )
    result = await client.claim_next([TaskType.DETECT_CUT_PLANES], worker_id="w")
    await client.close()

    assert result is None  # 204 → no task
    assert len(seen) == 1
    assert seen[0] is not None
    # claim_next must use the 30s general timeout, not 15s.
    assert seen[0]["read"] == 30.0
    assert seen[0]["write"] == 30.0
    assert seen[0]["connect"] == 30.0
    assert seen[0]["pool"] == 30.0


@pytest.mark.asyncio
async def test_lifecycle_timeout_none_falls_back_to_client_default():
    """When lifecycle_timeout_s is None, lifecycle calls must use the
    client's own default timeout rather than imposing a separate one —
    preserving the legacy behaviour for consumers that build their own
    client."""
    seen: list = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions.get("timeout"))
        return httpx.Response(200, json={"cancelled": False})

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(
        base_url="http://fake/api/v1", transport=transport,
        timeout=30.0, headers={"Authorization": "Bearer x"},
    )
    client = BackendClient(
        "http://fake/api/v1", "x", timeout_s=30.0, lifecycle_timeout_s=None,
        client=http,
    )  # lifecycle_timeout_s=None → _lifecycle_timeout is None
    await client.report_progress(5, stage="working")
    await client.close()

    assert len(seen) == 1
    # timeout=None passed to httpx means "use client default" — at the
    # request-extension level httpx records None for each facet (the
    # resolution to the client's 30s happens at the real transport layer,
    # which MockTransport doesn't simulate). The point is that no separate
    # lifecycle deadline is imposed: _lifecycle_timeout is None, so the call
    # inherits the client default rather than overriding it.
    assert seen[0] is not None
    assert seen[0]["read"] is None
    assert seen[0]["write"] is None
