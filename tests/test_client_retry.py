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
) -> BackendClient:
    """Build a real BackendClient backed by a MockTransport handler.

    ``retry_backoff_s`` defaults to 0 so the exponential-backoff sleeps are
    instant — the timing is verified separately via a mocked ``asyncio.sleep``.
    """
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(
        base_url="http://fake/api/v1",
        transport=transport,
        headers={"Authorization": "Bearer x"},
    )
    return BackendClient(
        "http://fake/api/v1",
        "x",
        client=http,
        max_retries=max_retries,
        retry_backoff_s=retry_backoff_s,
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
