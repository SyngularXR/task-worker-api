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
import json
import random

import httpx
import pytest

from task_worker_api.client import _DOWNLOAD_CHUNK_BYTES, BackendClient
from task_worker_api.enums import TaskType
from task_worker_api.errors import TaskCancelled


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _client_with_handler(
    handler,
    *,
    max_retries: int = 4,
    retry_backoff_s: float = 0.0,
    retry_backoff_max_s: float | None = None,
    retry_sleep_budget_s: float | None = None,
    retry_jitter: bool = False,
) -> BackendClient:
    """Build a real BackendClient backed by a MockTransport handler.

    ``retry_backoff_s`` defaults to 0 so the exponential-backoff sleeps are
    instant — the timing is verified separately via a mocked ``asyncio.sleep``.
    Jitter defaults to False so timing-assertion tests stay deterministic; the
    jitter behaviour itself is covered by dedicated _backoff_delay unit tests.

    ``retry_sleep_budget_s`` defaults to ``None`` — the SDK default, no total
    sleep budget — so tests that don't name it exercise the unbounded legacy
    retry behaviour. Budget tests pass a value explicitly.
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
    if retry_sleep_budget_s is not None:
        kwargs["retry_sleep_budget_s"] = retry_sleep_budget_s
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
async def test_download_file_writes_off_the_event_loop_in_1mb_chunks(
    tmp_path, monkeypatch,
):
    """Disk writes must run in a worker thread, on 1 MB chunk boundaries.

    ``aiter_bytes()`` yields whatever the transport hands over (~64 KB), and
    the write of each one used to run inline: a multi-GB PLY/splat on slow
    storage froze the event loop for the whole download, so the heartbeat
    stopped ticking (the sweeper reclaims the task), the CancelGuard poll
    froze, and a hybrid-mode FastAPI app stopped serving. Writing every wire
    chunk off-loop instead would be tens of thousands of dispatches per GB,
    hence the re-chunking.

    The wire chunks below are deliberately small (64 KB, the realistic
    transport size), so the recorded write sizes distinguish "buffered to
    1 MB" from "one write per wire chunk".
    """
    import threading

    from task_worker_api import client as client_mod

    wire_chunk = 64 * 1024
    n_wire_chunks = 40  # 2.5 MB → two full 1 MB writes plus a partial tail
    payload = bytes(range(256)) * (wire_chunk // 256)

    async def body():
        for _ in range(n_wire_chunks):
            yield payload

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body())

    loop_thread = threading.current_thread()
    writes: list[tuple[int, threading.Thread]] = []
    real_open = open

    class _SpyFile:
        def __init__(self, f):
            self._f = f

        def write(self, data):
            writes.append((len(data), threading.current_thread()))
            return self._f.write(data)

        def close(self):
            return self._f.close()

        # The pre-fix code used ``with open(dest, "wb") as f``; supporting the
        # protocol keeps this test failing on its actual assertions there
        # rather than on a missing ``__exit__``.
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._f.close()
            return False

    def spy_open(path, mode, *a, **kw):
        return _SpyFile(real_open(path, mode, *a, **kw))

    # ``client.py`` calls the builtin ``open`` unqualified, so a module-level
    # name shadows it for the duration of the test only.
    monkeypatch.setattr(client_mod, "open", spy_open, raising=False)

    client = _client_with_handler(handler)
    dest = tmp_path / "scene.ply"
    await client.download_file(5, "scene.ply", dest)
    await client.close()

    assert writes, "download wrote nothing"
    assert all(t is not loop_thread for _, t in writes), (
        "download_file wrote to disk on the event-loop thread; a multi-GB "
        "transfer on slow storage freezes the heartbeat and the cancel poll"
    )
    sizes = [n for n, _ in writes]
    assert all(n >= _DOWNLOAD_CHUNK_BYTES for n in sizes[:-1]), (
        f"wire chunks must be buffered to {_DOWNLOAD_CHUNK_BYTES} B before "
        f"writing, got {sizes}"
    )
    assert 0 < sizes[-1] <= _DOWNLOAD_CHUNK_BYTES
    assert sum(sizes) == wire_chunk * n_wire_chunks
    # Buffering must not corrupt or reorder the body.
    assert dest.read_bytes() == payload * n_wire_chunks


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


# -----------------------------------------------------------------------
# download_file cancel — a user cancel during a multi-GB input stream must
# abort at the next chunk boundary. prepare_inputs only checked between
# batch files, so a single-file input set (a lone colmap-splat PLY,
# a Neural-Canvas splat) streamed to completion after the cancel.
# -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_file_aborts_mid_stream_when_cancel_set(tmp_path):
    """A cancel that lands partway through the stream must raise
    TaskCancelled at the next chunk boundary — not drain the whole body.

    The handler counts the chunks it hands out, so draining the body to
    completion (the pre-fix behaviour) is distinguishable from aborting.

    The chunks are deliberately tiny (1 KB, a 10 KB body — far under the
    ``_DOWNLOAD_CHUNK_BYTES`` write buffer) and the boundary is a wire chunk,
    not a write: batching disk writes must not batch the cancel check with
    them. Iterating ``aiter_bytes(_DOWNLOAD_CHUNK_BYTES)`` instead would
    buffer this whole body into one chunk and drain it in full before the
    check ever ran, and on a real transfer would hide a cancel for as long as
    a slow or stalled response takes to fill 1 MB.
    """
    chunks_sent = {"n": 0}
    cancelled = asyncio.Event()

    async def body():
        for _ in range(10):
            chunks_sent["n"] += 1
            if chunks_sent["n"] == 2:
                # A CancelGuard poll observing the user's cancel, mid-stream.
                cancelled.set()
            yield b"x" * 1024

    requests = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        requests["n"] += 1
        return httpx.Response(200, content=body())

    client = _client_with_handler(handler, max_retries=4)
    dest = tmp_path / "out.ply"
    with pytest.raises(TaskCancelled, match="cancelled by user"):
        await client.download_file(5, "scene.ply", dest, cancelled=cancelled)
    await client.close()

    assert chunks_sent["n"] < 10, (
        "download must abort at a wire-chunk boundary, not stream the rest "
        "of a multi-GB file to a task the user already cancelled"
    )
    # A cancel is a failure like any other for cleanup purposes: no partial
    # input may survive for a retried task to mistake for a complete one.
    assert not dest.exists()


@pytest.mark.asyncio
async def test_download_file_cancel_does_not_consume_retry_budget(tmp_path):
    """TaskCancelled must pass straight through ``_retry``.

    If the cancel were treated as transient, the client would re-issue the
    GET up to ``max_retries`` times — re-streaming the very file the fix
    exists to stop streaming.
    """
    requests = {"n": 0}
    cancelled = asyncio.Event()

    async def body():
        cancelled.set()
        yield b"first-chunk"
        yield b"second-chunk"

    async def handler(request: httpx.Request) -> httpx.Response:
        requests["n"] += 1
        return httpx.Response(200, content=body())

    client = _client_with_handler(handler, max_retries=4)
    with pytest.raises(TaskCancelled):
        await client.download_file(
            5, "scene.ply", tmp_path / "out.ply", cancelled=cancelled,
        )
    await client.close()

    assert requests["n"] == 1


@pytest.mark.asyncio
async def test_download_file_checks_cancel_before_request(tmp_path):
    """An already-set event must abort before the GET goes out at all —
    a cancel detected between batch files shouldn't open the next stream."""
    requests = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        requests["n"] += 1
        return httpx.Response(200, content=b"body")

    cancelled = asyncio.Event()
    cancelled.set()
    client = _client_with_handler(handler)
    with pytest.raises(TaskCancelled):
        await client.download_file(
            5, "scene.ply", tmp_path / "out.ply", cancelled=cancelled,
        )
    await client.close()

    assert requests["n"] == 0


@pytest.mark.asyncio
async def test_download_file_unset_cancel_event_streams_full_body(tmp_path):
    """Supplying an event that never fires must not perturb the download —
    pins the polarity of the check (an inverted test would abort here)."""
    body = b"\x00\x01\x02" * 1000

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    client = _client_with_handler(handler)
    dest = tmp_path / "out.ply"
    await client.download_file(5, "scene.ply", dest, cancelled=asyncio.Event())
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


# -----------------------------------------------------------------------
# upload_file cancel — a user cancel during a multi-GB output stream must
# abort the in-flight PUT. upload_outputs only checked between batch files,
# so a single-file output set (a lone colmap-splat PLY, a Neural-Canvas
# splat) streamed to completion after the cancel. The upload-side
# counterpart of the download_file cancel tests above.
# -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_file_aborts_in_flight_put_when_cancel_set(tmp_path):
    """A cancel that lands while the body is streaming must abort the PUT —
    not deliver the file to a task the user already cancelled.

    The handler parks (standing in for a multi-minute body upload) and only
    records delivery afterwards, so streaming to completion (the pre-fix
    behaviour) is distinguishable from aborting.
    """
    src = tmp_path / "output.ply"
    src.write_bytes(b"pretend-multi-GB-PLY")

    started = asyncio.Event()
    cancelled = asyncio.Event()
    delivered = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.sleep(30)  # the body still streaming to the backend
        delivered["n"] += 1
        return httpx.Response(200)

    async def cancel_once_in_flight() -> None:
        # A CancelGuard poll observing the user's cancel, mid-upload.
        await started.wait()
        cancelled.set()

    client = _client_with_handler(handler, max_retries=4)
    poll = asyncio.create_task(cancel_once_in_flight())
    with pytest.raises(TaskCancelled, match="cancelled by user"):
        # wait_for bounds the failure mode: an upload that ignores the event
        # fails the test instead of parking CI on the 30s handler.
        await asyncio.wait_for(
            client.upload_file(9, "output.ply", src, cancelled=cancelled), 5,
        )
    await poll
    await client.close()

    assert delivered["n"] == 0, (
        "upload must abort in flight, not stream the rest of a multi-GB file "
        "to a task the user already cancelled"
    )


@pytest.mark.asyncio
async def test_upload_file_cancel_does_not_consume_retry_budget(tmp_path):
    """TaskCancelled must pass straight through ``_retry``.

    If the cancel were treated as transient, the client would re-issue the
    PUT up to ``max_retries`` times — re-sending the very file the fix
    exists to stop sending.
    """
    src = tmp_path / "output.ply"
    src.write_bytes(b"x" * 64)

    requests = {"n": 0}
    cancelled = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        requests["n"] += 1
        cancelled.set()
        await asyncio.sleep(30)
        return httpx.Response(200)  # pragma: no cover — cancelled first

    client = _client_with_handler(handler, max_retries=4)
    with pytest.raises(TaskCancelled):
        await asyncio.wait_for(
            client.upload_file(9, "output.ply", src, cancelled=cancelled), 5,
        )
    await client.close()

    assert requests["n"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["download", "upload"])
async def test_file_transfer_cancel_interrupts_retry_backoff(
    monkeypatch, tmp_path, direction,
):
    """A cancel must interrupt retry backoff, not wait for its delay."""
    sleeping = asyncio.Event()
    release = asyncio.Event()

    async def blocked_sleep(delay):
        sleeping.set()
        await release.wait()

    monkeypatch.setattr(asyncio, "sleep", blocked_sleep)

    requests = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        requests["n"] += 1
        return httpx.Response(503)

    client = _client_with_handler(handler, max_retries=4)
    cancelled = asyncio.Event()
    if direction == "download":
        operation = client.download_file(
            9, "file.ply", tmp_path / "file.ply", cancelled=cancelled,
        )
    else:
        src = tmp_path / "file.ply"
        src.write_bytes(b"output")
        operation = client.upload_file(
            9, "file.ply", src, cancelled=cancelled,
        )

    transfer = asyncio.create_task(operation)
    await asyncio.wait_for(sleeping.wait(), 1)
    cancelled.set()

    with pytest.raises(TaskCancelled):
        await asyncio.wait_for(transfer, 1)
    await client.close()

    assert requests["n"] == 1


@pytest.mark.asyncio
async def test_upload_file_drains_put_when_caller_is_cancelled(tmp_path):
    """Worker shutdown: cancelling the *caller* must not leave the PUT running.

    ``Task.cancel()`` only requests cancellation, so returning straight after
    it would unwind ``upload_file`` — closing ``src`` and letting the caller
    close the client — while the request was still to be resumed for its own
    teardown. The handler's cleanup needs a loop turn (as a real connection
    teardown does), so it has only run by the time ``upload_file`` finishes
    if the cancel was actually awaited to completion.
    """
    src = tmp_path / "output.ply"
    src.write_bytes(b"pretend-multi-GB-PLY")

    started = asyncio.Event()
    torn_down = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        started.set()
        try:
            await asyncio.sleep(30)  # the body still streaming to the backend
        except asyncio.CancelledError:
            await asyncio.sleep(0)  # connection teardown, one loop turn
            torn_down.set()
            raise
        return httpx.Response(200)  # pragma: no cover — cancelled first

    client = _client_with_handler(handler)
    upload = asyncio.create_task(
        client.upload_file(9, "output.ply", src, cancelled=asyncio.Event()),
    )
    await started.wait()

    upload.cancel()  # the worker shutting down mid-upload
    with pytest.raises(asyncio.CancelledError):
        await upload

    assert torn_down.is_set(), (
        "the in-flight PUT must be awaited to completion, not left running "
        "detached after upload_file closed src and the worker moved on"
    )
    await client.close()


@pytest.mark.asyncio
async def test_upload_file_checks_cancel_before_request(tmp_path):
    """An already-set event must abort before the PUT goes out at all —
    a cancel detected between batch files shouldn't open the next upload."""
    src = tmp_path / "output.ply"
    src.write_bytes(b"x")

    requests = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        requests["n"] += 1
        return httpx.Response(200)

    cancelled = asyncio.Event()
    cancelled.set()
    client = _client_with_handler(handler)
    with pytest.raises(TaskCancelled):
        await client.upload_file(9, "output.ply", src, cancelled=cancelled)
    await client.close()

    assert requests["n"] == 0


@pytest.mark.asyncio
async def test_upload_file_unset_cancel_event_sends_full_body(tmp_path):
    """Supplying an event that never fires must not perturb the upload —
    pins the polarity of the check (an inverted test would abort here) and
    proves the race hands back the real response, not a stand-in."""
    src = tmp_path / "output.ply"
    src.write_bytes(b"result-bytes")

    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(200)

    client = _client_with_handler(handler)
    await client.upload_file(
        9, "output.ply", src, cancelled=asyncio.Event(),
    )
    await client.close()

    assert b"result-bytes" in captured["body"]


@pytest.mark.asyncio
async def test_upload_file_cancel_still_raises_non_transient_status(tmp_path):
    """A live-but-unset event must not swallow the response's own errors:
    the raced request's result is still status-checked."""
    src = tmp_path / "output.ply"
    src.write_bytes(b"x")

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="backend down")

    client = _client_with_handler(handler)
    with pytest.raises(httpx.HTTPStatusError):
        await client.upload_file(
            9, "output.ply", src, cancelled=asyncio.Event(),
        )
    await client.close()


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
# Transient gateway retry — the backend sits behind nginx; a 502/503/504
# almost always means the Flask upstream restarted or is momentarily
# overloaded. Previously every HTTPStatusError (including these transient
# gateway codes) surfaced immediately, failing the task on a blip that clears
# in seconds. Now 502/503/504 are retried with the same backoff as transport
# errors; 500 and non-429 4xx still surface immediately (500 = app logic
# error, 4xx = client error — retrying won't help).
#
# 429 (Too Many Requests) is also retried: the shared backend rate-limits
# lifecycle calls (complete/fail/progress) under fleet burst load, and
# dropping the terminal status on a 429 would strand the task in_progress
# until the sweeper reclaims it.
# -----------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 502, 503, 504])
async def test_request_retries_on_transient_gateway_status_then_succeeds(status):
    """A 429/502/503/504 from the backend is transient (rate-limit /
    upstream restart/overload) and must be retried with backoff, not failed
    immediately."""
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


# -----------------------------------------------------------------------
# 429 (Too Many Requests) retry — the shared backend serves 3+ workers and
# can rate-limit a lifecycle call under burst load. A terminal complete/fail
# that hits a 429 must be retried with backoff; otherwise the task is left
# stuck in_progress until the sweeper reclaims it.
# -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_retries_on_429_then_succeeds():
    """A 429 on a terminal complete call must be retried, not dropped.

    This is the core reliability scenario: the backend rate-limits the
    complete request, and the worker rides through it with backoff so the
    task reaches its terminal status instead of being stranded in_progress.
    """
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, text="Too Many Requests")
        return httpx.Response(200)

    client = _client_with_handler(handler, max_retries=4)
    await client.complete(7, {"output": "done"})
    await client.close()

    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_fail_retries_on_429_then_succeeds():
    """A 429 on a terminal fail call must be retried with backoff so the
    task reaches its failed status rather than being stranded in_progress."""
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 2:
            return httpx.Response(429, text="Too Many Requests")
        return httpx.Response(200)

    client = _client_with_handler(handler, max_retries=4)
    await client.fail(7, "boom")
    await client.close()

    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_request_retries_429_then_raises_after_exhaustion():
    """When every attempt returns 429, the last HTTPStatusError is re-raised
    to the caller — the same exhaustion contract as 5xx gateway codes."""
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, text="Too Many Requests")

    client = _client_with_handler(handler, max_retries=3)
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await client.claim_next([TaskType.DETECT_CUT_PLANES], worker_id="w")
    await client.close()

    assert calls["n"] == 3
    assert exc_info.value.response.status_code == 429


# -----------------------------------------------------------------------
# poll_cancel_status — one-shot cancel-poll with NO retries. The
# CancelGuard polls /tasks/{id}/cancel-status on its own schedule
# (cancel_poll_interval_s, default 2s). Previously it called
# get_cancel_status → _request → _retry, which retries 4× with
# exponential backoff; a degraded backend could blind the guard for
# ~50s while the worker kept computing on a cancelled task. The new
# method does a single GET with cancel_timeout_s and surfaces errors
# immediately — the guard catches them and retries on its own next tick.
# -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_cancel_status_does_not_retry_transport_error():
    """A transient TransportError must surface immediately — exactly one
    attempt, no backoff. The CancelGuard retries on its own poll interval."""
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.TransportError("transient hiccup")

    client = _client_with_handler(handler, max_retries=4)
    with pytest.raises(httpx.TransportError, match="transient hiccup"):
        await client.poll_cancel_status(7)
    await client.close()

    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_poll_cancel_status_does_not_retry_transient_5xx():
    """A 503 must surface immediately — the guard retries on its own
    schedule, not the client's backoff loop."""
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, text="Service Unavailable")

    client = _client_with_handler(handler, max_retries=4)
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await client.poll_cancel_status(7)
    await client.close()

    assert calls["n"] == 1
    assert exc_info.value.response.status_code == 503


@pytest.mark.asyncio
async def test_poll_cancel_status_does_not_retry_429():
    """A 429 must surface immediately — same one-shot contract as 5xx."""
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, text="Too Many Requests")

    client = _client_with_handler(handler, max_retries=4)
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await client.poll_cancel_status(7)
    await client.close()

    assert calls["n"] == 1
    assert exc_info.value.response.status_code == 429


@pytest.mark.asyncio
async def test_poll_cancel_status_does_not_retry_timeout():
    """A TimeoutException must surface immediately — no backoff sleep."""
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.TimeoutException("read timed out")

    client = _client_with_handler(handler, max_retries=4)
    with pytest.raises(httpx.TimeoutException):
        await client.poll_cancel_status(7)
    await client.close()

    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_poll_cancel_status_no_backoff_sleeps(monkeypatch):
    """The one-shot method must not schedule any backoff sleeps — the
    guard owns the retry cadence."""
    sleeps: list[float] = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TransportError("keep failing")

    client = _client_with_handler(handler, max_retries=4)
    with pytest.raises(httpx.TransportError):
        await client.poll_cancel_status(7)
    await client.close()

    assert sleeps == []


@pytest.mark.asyncio
async def test_poll_cancel_status_uses_cancel_timeout_not_general():
    """poll_cancel_status must apply cancel_timeout_s to the request, not
    the 30s general request timeout — same deadline as get_cancel_status."""
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
        "http://fake/api/v1", "x", timeout_s=30.0, cancel_timeout_s=5.0,
        client=http,
    )
    result = await client.poll_cancel_status(7)
    await client.close()

    assert result == {"cancelled": False}
    assert len(seen) == 1
    assert seen[0] is not None
    assert seen[0]["read"] == 5.0
    assert seen[0]["write"] == 5.0
    assert seen[0]["connect"] == 5.0
    assert seen[0]["pool"] == 5.0


@pytest.mark.asyncio
async def test_poll_cancel_status_returns_json_body():
    """A successful one-shot poll returns the parsed JSON body."""
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"cancelled": True})

    client = _client_with_handler(handler)
    result = await client.poll_cancel_status(7)
    await client.close()

    assert result == {"cancelled": True}


@pytest.mark.asyncio
async def test_poll_cancel_status_raises_on_500():
    """A 500 is non-transient — must surface immediately (one attempt)."""
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, text="Internal Server Error")

    client = _client_with_handler(handler, max_retries=4)
    with pytest.raises(httpx.HTTPStatusError):
        await client.poll_cancel_status(7)
    await client.close()

    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_poll_cancel_status_cancel_timeout_none_falls_back():
    """When cancel_timeout_s is None, poll_cancel_status must use the
    client's own default timeout — same legacy fallback as
    get_cancel_status."""
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
        "http://fake/api/v1", "x", timeout_s=30.0, cancel_timeout_s=None,
        client=http,
    )
    await client.poll_cancel_status(5)
    await client.close()

    assert len(seen) == 1
    assert seen[0] is not None
    assert seen[0]["read"] is None
    assert seen[0]["write"] is None


# -----------------------------------------------------------------------
# report_progress_once — one-shot progress PUT with NO retries, for the
# immediate report ProgressReporter.update() emits on the handler's
# critical path. Going through _retry there meant a degraded backend could
# stall the handler for ~75s (4 attempts x 15s lifecycle timeout plus
# backoff) on a single progress update. The background heartbeat keeps
# using the retried report_progress, so updated_at still rides out blips.
# -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_report_progress_once_does_not_retry_transport_error():
    """A transient TransportError must surface immediately — exactly one
    attempt, no backoff. The next heartbeat re-sends the state."""
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.TransportError("transient hiccup")

    client = _client_with_handler(handler, max_retries=4)
    with pytest.raises(httpx.TransportError, match="transient hiccup"):
        await client.report_progress_once(7, stage="working")
    await client.close()

    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_report_progress_once_does_not_retry_transient_5xx():
    """A 503 must surface immediately — unlike report_progress, which
    retries it."""
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, text="Service Unavailable")

    client = _client_with_handler(handler, max_retries=4)
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await client.report_progress_once(7, stage="working")
    await client.close()

    assert calls["n"] == 1
    assert exc_info.value.response.status_code == 503


@pytest.mark.asyncio
async def test_report_progress_once_does_not_retry_429():
    """A 429 must surface immediately — same one-shot contract as 5xx."""
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, text="Too Many Requests")

    client = _client_with_handler(handler, max_retries=4)
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await client.report_progress_once(7, stage="working")
    await client.close()

    assert calls["n"] == 1
    assert exc_info.value.response.status_code == 429


@pytest.mark.asyncio
async def test_report_progress_once_no_backoff_sleeps(monkeypatch):
    """The one-shot report must not schedule any backoff sleeps — that
    sleep would be spent on the handler's critical path."""
    sleeps: list[float] = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("read timed out")

    client = _client_with_handler(handler, max_retries=4)
    with pytest.raises(httpx.TimeoutException):
        await client.report_progress_once(7, stage="working")
    await client.close()

    assert sleeps == []


@pytest.mark.asyncio
async def test_report_progress_once_sends_same_body_as_retried():
    """Same wire format as report_progress — path, method, and body
    (including the kill_handle) must be byte-identical."""
    seen: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append({
            "method": request.method,
            "path": request.url.path,
            "body": json.loads(request.content),
        })
        return httpx.Response(200, json={"cancelled": True})

    kill_handle = {"pid": None, "container": None, "host": "remote"}
    client = _client_with_handler(handler)
    result = await client.report_progress_once(
        7, stage="rendering", current=3, total=10, kill_handle=kill_handle,
    )
    await client.report_progress(
        7, stage="rendering", current=3, total=10, kill_handle=kill_handle,
    )
    await client.close()

    assert result == {"cancelled": True}
    assert len(seen) == 2
    assert seen[0] == seen[1]
    assert seen[0]["method"] == "PUT"
    assert seen[0]["path"] == "/api/v1/tasks/7/progress"
    assert seen[0]["body"] == {
        "stage": "rendering", "current": 3, "total": 10,
        "kill_handle": kill_handle,
    }


@pytest.mark.asyncio
async def test_report_progress_once_omits_absent_kill_handle():
    """No kill_handle → the key is absent, matching report_progress."""
    seen: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={})

    client = _client_with_handler(handler)
    assert await client.report_progress_once(7, stage="working") == {}
    await client.close()

    assert seen == [{"stage": "working", "current": 0, "total": 0}]


@pytest.mark.asyncio
async def test_report_progress_once_uses_lifecycle_timeout_not_general():
    """The one-shot report must apply lifecycle_timeout_s, not the 30s
    general request timeout — same deadline as report_progress."""
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
        "http://fake/api/v1", "x", timeout_s=30.0, lifecycle_timeout_s=15.0,
        client=http,
    )
    await client.report_progress_once(7, stage="working")
    await client.close()

    assert len(seen) == 1
    assert seen[0] is not None
    assert seen[0]["read"] == 15.0
    assert seen[0]["write"] == 15.0
    assert seen[0]["connect"] == 15.0
    assert seen[0]["pool"] == 15.0


@pytest.mark.asyncio
async def test_report_progress_once_raises_on_500():
    """A 500 is non-transient — must surface immediately (one attempt)."""
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, text="Internal Server Error")

    client = _client_with_handler(handler, max_retries=4)
    with pytest.raises(httpx.HTTPStatusError):
        await client.report_progress_once(7, stage="working")
    await client.close()

    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_report_progress_still_retries_transient_5xx():
    """The retried path is unchanged — the heartbeat still rides through a
    backend blip that the one-shot path now surfaces immediately."""
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, text="Service Unavailable")
        return httpx.Response(200, json={"cancelled": False})

    client = _client_with_handler(handler, max_retries=4)
    assert await client.report_progress(7, stage="working") == {"cancelled": False}
    await client.close()

    assert calls["n"] == 3


# -----------------------------------------------------------------------
# Terminal-report retry — complete/fail widen the transient set to include
# 500 and raise the attempt budget to at least 6. A 500 on a terminal
# report can mean the backend's own dependency died mid-write (e.g. a
# Postgres I/O error); dropping the report orphans a fully computed
# outcome as a RUNNING zombie until the sweeper. Both terminal routes are
# idempotent guarded transitions, so re-PUTting is safe. All other calls
# (claim/progress/files) keep 500 non-retryable.
# -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fail_retries_on_500_then_succeeds():
    """A 500 on PUT /tasks/{id}/fail is retried — the terminal report must
    survive a backend whose DB hiccuped mid-write."""
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        assert request.url.path == "/api/v1/tasks/691/fail"
        if calls["n"] < 3:
            return httpx.Response(500, text="Internal Server Error")
        return httpx.Response(200)

    client = _client_with_handler(handler, max_retries=4)
    await client.fail(691, "boom")
    await client.close()

    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_complete_retries_on_500_then_succeeds():
    """A 500 on PUT /tasks/{id}/complete is retried, same as fail()."""
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, text="Internal Server Error")
        return httpx.Response(200)

    client = _client_with_handler(handler, max_retries=4)
    await client.complete(7, {"output": "done"})
    await client.close()

    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_fail_raises_after_exhausting_terminal_attempts():
    """When every fail() attempt returns 500, the last HTTPStatusError is
    re-raised after the raised terminal budget (max(max_retries, 6))."""
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, text="still dead")

    client = _client_with_handler(handler, max_retries=4)
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await client.fail(691, "boom")
    await client.close()

    assert exc_info.value.response.status_code == 500
    # Attempt floor: max(max_retries=4, 6) == 6.
    assert calls["n"] == 6


@pytest.mark.asyncio
async def test_terminal_attempt_budget_respects_higher_max_retries():
    """A consumer-configured max_retries above the floor wins: the terminal
    budget is max(max_retries, 6), not a hard-coded 6."""
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, text="still dead")

    client = _client_with_handler(handler, max_retries=8)
    with pytest.raises(httpx.HTTPStatusError):
        await client.complete(7, {"output": "x"})
    await client.close()

    assert calls["n"] == 8


@pytest.mark.asyncio
async def test_report_progress_still_does_not_retry_500():
    """The 500 widening is terminal-only: report_progress keeps the
    fail-fast contract (500 = app error; heartbeats must not block the
    polling loop on retry budget)."""
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, text="Internal Server Error")

    client = _client_with_handler(handler, max_retries=4)
    with pytest.raises(httpx.HTTPStatusError):
        await client.report_progress(7, stage="working", current=1, total=2)
    await client.close()

    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_fail_still_retries_transport_errors():
    """The widened terminal budget also applies to transport errors — a
    connection refused during a backend restart gets the full window."""
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 5:
            raise httpx.TransportError("connection refused")
        return httpx.Response(200)

    client = _client_with_handler(handler, max_retries=4)
    # 5 attempts needed; plain max_retries=4 would have exhausted, but the
    # terminal floor of 6 rides through.
    await client.fail(691, "boom")
    await client.close()

    assert calls["n"] == 5


# -----------------------------------------------------------------------
# Retry-After — a transient status response can name its own retry instant
# (RFC 9110: delta-seconds or HTTP-date). The shared backend rate-limits
# lifecycle calls under fleet burst load; answering a 429's Retry-After=N
# with the SDK's own 2s/4s/8s schedule burns the attempt budget *inside*
# the rate-limit window, so a terminal complete/fail report can exhaust all
# 6 attempts while still throttled and strand the task in_progress until
# the sweeper reclaims it. When the header is present and parseable the
# inter-attempt delay is the server's; absent/unparseable falls back to the
# capped-jittered exponential schedule (identical behaviour to before).
# -----------------------------------------------------------------------


def _sleep_recorder(monkeypatch) -> list:
    """Patch asyncio.sleep to record delays instead of waiting."""
    sleeps: list[float] = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return sleeps


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 503])
async def test_retry_after_seconds_replaces_backoff(monkeypatch, status):
    """A parseable delta-seconds Retry-After sets the inter-attempt delay,
    overriding the 2/4/8 exponential schedule."""
    sleeps = _sleep_recorder(monkeypatch)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers={"Retry-After": "7"}, text="slow down")

    client = _client_with_handler(handler, max_retries=4, retry_backoff_s=2.0)
    with pytest.raises(httpx.HTTPStatusError):
        await client.claim_next([TaskType.DETECT_CUT_PLANES], worker_id="w")
    await client.close()

    # Would have been [2.0, 4.0, 8.0] without the header.
    assert sleeps == [7.0, 7.0, 7.0]


@pytest.mark.asyncio
async def test_retry_after_jitter_never_retries_early(monkeypatch):
    """Retry-After jitter is positive-only, so workers spread out without
    violating the server's minimum delay."""
    sleeps = _sleep_recorder(monkeypatch)

    def upper_jitter(low, high):
        assert (low, high) == (0.0, 0.25)
        return high

    monkeypatch.setattr(random, "uniform", upper_jitter)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "8"}, text="slow down")

    client = _client_with_handler(handler, max_retries=2, retry_jitter=True)
    with pytest.raises(httpx.HTTPStatusError):
        await client.claim_next([TaskType.DETECT_CUT_PLANES], worker_id="w")
    await client.close()

    assert sleeps == [10.0]


@pytest.mark.asyncio
async def test_retry_after_http_date_replaces_backoff(monkeypatch):
    """The HTTP-date form is honoured too, converted to a delay from now."""
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    sleeps = _sleep_recorder(monkeypatch)
    when = format_datetime(
        datetime.now(timezone.utc) + timedelta(seconds=30), usegmt=True,
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": when}, text="slow down")

    client = _client_with_handler(handler, max_retries=2, retry_backoff_s=2.0)
    with pytest.raises(httpx.HTTPStatusError):
        await client.claim_next([TaskType.DETECT_CUT_PLANES], worker_id="w")
    await client.close()

    # One sleep, ~30s (the header has whole-second resolution, so the delay
    # lands just under 30 — never the 2.0 the backoff schedule would give).
    assert len(sleeps) == 1
    assert 28.0 <= sleeps[0] <= 30.0


@pytest.mark.asyncio
@pytest.mark.parametrize("header", [
    # "-5" is malformed, not guidance: RFC 9110 delta-seconds is a
    # non-negative integer. ("0" *is* guidance — see the test below.)
    "soon", "", "2.5", "+5", "1_000", "-5", "not-a-date",
    # A malformed HTTP-date must not replace the expected HTTPStatusError with
    # a parser exception.
    pytest.param("Wed, 21 Oct " + "9" * 40 + " 07:28:00 GMT", id="oversized-year"),
])
async def test_retry_after_invalid_falls_back_to_backoff(monkeypatch, header):
    """An unparseable Retry-After leaves the exponential
    schedule untouched — malformed input must never make retries *faster*."""
    sleeps = _sleep_recorder(monkeypatch)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": header}, text="slow down")

    client = _client_with_handler(handler, max_retries=4, retry_backoff_s=2.0)
    with pytest.raises(httpx.HTTPStatusError):
        await client.claim_next([TaskType.DETECT_CUT_PLANES], worker_id="w")
    await client.close()

    assert sleeps == [2.0, 4.0, 8.0]


@pytest.mark.asyncio
async def test_retry_after_absent_keeps_exponential_backoff(monkeypatch):
    """No header → byte-identical behaviour to before this feature."""
    sleeps = _sleep_recorder(monkeypatch)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="Service Unavailable")

    client = _client_with_handler(handler, max_retries=4, retry_backoff_s=2.0)
    with pytest.raises(httpx.HTTPStatusError):
        await client.claim_next([TaskType.DETECT_CUT_PLANES], worker_id="w")
    await client.close()

    assert sleeps == [2.0, 4.0, 8.0]


@pytest.mark.asyncio
@pytest.mark.parametrize("header", [
    "0",
    # An HTTP-date already in the past says the same thing: the window the
    # server named has closed, so retry now.
    "Wed, 21 Oct 2020 07:28:00 GMT",
])
async def test_retry_after_zero_retries_immediately(monkeypatch, header):
    """Retry-After: 0 is guidance to retry immediately, and is honoured as
    such — it must not be downgraded to the exponential schedule."""
    sleeps = _sleep_recorder(monkeypatch)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": header}, text="slow down")

    client = _client_with_handler(handler, max_retries=4, retry_backoff_s=2.0)
    with pytest.raises(httpx.HTTPStatusError):
        await client.claim_next([TaskType.DETECT_CUT_PLANES], worker_id="w")
    await client.close()

    # Would have been [2.0, 4.0, 8.0] if a 0 were treated as "no guidance".
    assert sleeps == [0.0, 0.0, 0.0]


@pytest.mark.asyncio
async def test_retry_after_honoured_in_full_never_shortened(monkeypatch):
    """A normal Retry-After is slept in full, not shortened.

    Retrying before the instant the server named lands inside the window it
    just said was closed, so it burns an attempt that near-certainly fails.
    Only the dedicated remote-input safety ceiling may shorten the delay.
    """
    sleeps = _sleep_recorder(monkeypatch)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "120"}, text="slow down")

    client = _client_with_handler(
        handler, max_retries=3, retry_backoff_s=2.0, retry_backoff_max_s=300.0,
    )
    with pytest.raises(httpx.HTTPStatusError):
        await client.claim_next([TaskType.DETECT_CUT_PLANES], worker_id="w")
    await client.close()

    assert sleeps == [120.0, 120.0]


@pytest.mark.asyncio
async def test_retry_after_is_not_shortened_by_backoff_ceiling(monkeypatch):
    """The backoff cap must not move a retry before the server's deadline."""
    sleeps = _sleep_recorder(monkeypatch)
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                429, headers={"Retry-After": "3600"}, text="slow down",
            )
        return httpx.Response(200)

    client = _client_with_handler(
        handler, max_retries=3, retry_backoff_s=2.0, retry_backoff_max_s=30.0,
    )
    await client.complete(7, {"output": "done"})
    await client.close()

    assert sleeps == [3600.0]
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_retry_after_has_distinct_remote_input_cap(monkeypatch):
    """Absurd server guidance is bounded independently from backoff."""
    sleeps = _sleep_recorder(monkeypatch)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429, headers={"Retry-After": "86400"}, text="Too Many Requests",
        )

    client = _client_with_handler(
        handler, max_retries=2, retry_backoff_s=2.0, retry_backoff_max_s=30.0,
    )
    with pytest.raises(httpx.HTTPStatusError):
        await client.claim_next([TaskType.DETECT_CUT_PLANES], worker_id="w")
    await client.close()

    assert sleeps == [6 * 60 * 60]


@pytest.mark.asyncio
async def test_retry_after_does_not_extend_attempt_budget(monkeypatch):
    """Honouring the header changes the *delay*, not the attempt count."""
    _sleep_recorder(monkeypatch)
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, headers={"Retry-After": "5"}, text="slow down")

    client = _client_with_handler(handler, max_retries=3)
    with pytest.raises(httpx.HTTPStatusError):
        await client.claim_next([TaskType.DETECT_CUT_PLANES], worker_id="w")
    await client.close()

    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_terminal_report_waits_out_rate_limit_window(monkeypatch):
    """The motivating case: a throttled complete() must wait the window the
    backend named rather than burning its 6 attempts inside it."""
    sleeps = _sleep_recorder(monkeypatch)
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(
                429, headers={"Retry-After": "20"}, text="Too Many Requests",
            )
        return httpx.Response(200)

    client = _client_with_handler(handler, max_retries=4, retry_backoff_s=2.0)
    await client.complete(7, {"output": "done"})
    await client.close()

    assert calls["n"] == 3
    assert sleeps == [20.0, 20.0]


@pytest.mark.asyncio
async def test_retry_after_ignored_for_transport_errors(monkeypatch):
    """Transport errors have no response to read a header from — they keep
    the exponential schedule unconditionally."""
    sleeps = _sleep_recorder(monkeypatch)

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TransportError("connection refused")

    client = _client_with_handler(handler, max_retries=3, retry_backoff_s=2.0)
    with pytest.raises(httpx.TransportError):
        await client.claim_next([TaskType.DETECT_CUT_PLANES], worker_id="w")
    await client.close()

    assert sleeps == [2.0, 4.0]


def test_retry_after_delay_parses_both_rfc_forms():
    """Unit coverage for the parser: delta-seconds, HTTP-date, and every
    'no guidance' case that must fall back to the exponential schedule."""
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    from task_worker_api.client import _retry_after_delay

    def _resp(headers):
        return httpx.Response(429, headers=headers)

    assert _retry_after_delay(_resp({"Retry-After": "12"})) == 12.0
    # Header lookup is case-insensitive and tolerates surrounding whitespace.
    assert _retry_after_delay(_resp({"retry-after": " 12 "})) == 12.0

    future = format_datetime(
        datetime.now(timezone.utc) + timedelta(seconds=45), usegmt=True,
    )
    d = _retry_after_delay(_resp({"Retry-After": future}))
    assert d is not None and 43.0 <= d <= 45.0

    # "Retry now" (0.0) is a real answer and must stay distinct from None
    # ("no guidance, use our own schedule").
    past = format_datetime(
        datetime.now(timezone.utc) - timedelta(seconds=45), usegmt=True,
    )
    assert _retry_after_delay(_resp({"Retry-After": past})) == 0.0
    assert _retry_after_delay(_resp({"Retry-After": "0"})) == 0.0
    assert _retry_after_delay(_resp({})) is None
    # Negative delta-seconds is malformed (RFC 9110: non-negative), not "now".
    assert _retry_after_delay(_resp({"Retry-After": "-30"})) is None
    assert _retry_after_delay(_resp({"Retry-After": "later"})) is None
    # Oversized delta-seconds are capped before float conversion; malformed
    # dates are rejected without leaking parser exceptions.
    assert _retry_after_delay(
        _resp({"Retry-After": "1" + "0" * 5000}),
    ) == 6 * 60 * 60
    assert _retry_after_delay(
        _resp({"Retry-After": "Wed, 21 Oct " + "9" * 40 + " 07:28:00 GMT"}),
    ) is None


# -----------------------------------------------------------------------
# Total retry budget — the per-delay caps bound each sleep in isolation, but
# they multiply against the attempt budget. A persistently rate-limited
# backend answering every attempt with a long Retry-After (honoured in full
# by design, and capped only at 6h) can pin a terminal complete/fail report —
# 6-attempt floor, so five inter-attempt sleeps — for up to 5 × 6h = 30h on
# one call. The worker runs one task at a time, so that call blocks the whole
# polling loop: no claims, no cancel polls, no shutdown response.
# retry_sleep_budget_s bounds the sleeps one call may *start*: when the next
# delay doesn't fit what's left, the loop stops and re-raises instead of firing
# a futile request inside the window. It bounds admission, not wall clock — a
# started sleep is never interrupted (see the overrun test below). It is
# opt-in: the default is None (unbounded, exactly as before the knob existed),
# and 600s is the recommended value to enable.
# -----------------------------------------------------------------------


def test_init_rejects_non_positive_retry_total_max():
    """A non-positive budget is degenerate — fail fast, like the backoff cap."""
    with pytest.raises(ValueError, match="retry_sleep_budget_s must be"):
        BackendClient("http://fake", "x", retry_sleep_budget_s=0)
    with pytest.raises(ValueError, match="retry_sleep_budget_s must be"):
        BackendClient("http://fake", "x", retry_sleep_budget_s=-5)


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_init_rejects_non_finite_retry_total_max(value):
    """A NaN budget is worse than a bad one: every comparison against NaN is
    False, so it passes a bare ``<= 0`` check and then silently disables the
    limit it was meant to impose — the caller believes they bounded the wait
    and nothing bounds it. ``inf`` is an unbounded budget spelled as a number.
    ``None`` is the one supported way to opt out."""
    with pytest.raises(ValueError, match="retry_sleep_budget_s must be"):
        BackendClient("http://fake", "x", retry_sleep_budget_s=value)


def test_init_accepts_none_retry_total_max():
    """None disables the budget — valid (legacy unbounded behaviour)."""
    client = BackendClient("http://fake", "x", retry_sleep_budget_s=None)
    assert client.retry_sleep_budget_s is None


def test_init_defaults_retry_total_max_to_none():
    """The budget is opt-in: omitting it leaves retrying unbounded, so an
    existing consumer's behaviour is unchanged by upgrading the SDK."""
    assert BackendClient("http://fake", "x").retry_sleep_budget_s is None


@pytest.mark.asyncio
async def test_terminal_report_gives_up_instead_of_sleeping_for_hours(monkeypatch):
    """The motivating case: with the budget enabled, a complete() throttled
    with Retry-After: 6h must not sleep it out — one such delay already
    exceeds the whole budget, so the call gives up immediately rather than
    pinning the polling loop for 30h."""
    sleeps = _sleep_recorder(monkeypatch)
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            429, headers={"Retry-After": "21600"}, text="Too Many Requests",
        )

    client = _client_with_handler(
        handler, max_retries=4, retry_backoff_s=2.0, retry_sleep_budget_s=600.0,
    )
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await client.complete(7, {"output": "done"})
    await client.close()

    # One attempt, no sleep — and the caller sees the same error it would
    # have seen after exhausting the attempt budget, so the task is re-queued
    # by the sweeper exactly as before.
    assert exc_info.value.response.status_code == 429
    assert calls["n"] == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_total_budget_accumulates_across_attempts(monkeypatch):
    """Slept time accumulates: retries continue while they fit the budget
    (a delay landing exactly on it still fits) and stop when the next one
    would overrun it."""
    sleeps = _sleep_recorder(monkeypatch)
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            429, headers={"Retry-After": "200"}, text="Too Many Requests",
        )

    client = _client_with_handler(
        handler, max_retries=6, retry_backoff_s=2.0, retry_sleep_budget_s=400.0,
    )
    with pytest.raises(httpx.HTTPStatusError):
        await client.claim_next([TaskType.DETECT_CUT_PLANES], worker_id="w")
    await client.close()

    # 200 + 200 exactly fills the 400s budget; the third would overrun it.
    assert sleeps == [200.0, 200.0]
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_budget_exhaustion_logs_warning(monkeypatch, caplog):
    """Operators must be able to see why a report gave up while the backend
    is plainly still up — so early exhaustion logs at WARNING, not DEBUG."""
    _sleep_recorder(monkeypatch)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503, headers={"Retry-After": "900"}, text="Service Unavailable",
        )

    client = _client_with_handler(
        handler, max_retries=4, retry_sleep_budget_s=600.0,
    )
    with caplog.at_level("WARNING"):
        with pytest.raises(httpx.HTTPStatusError):
            await client.claim_next([TaskType.DETECT_CUT_PLANES], worker_id="w")
    await client.close()

    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("retry_sleep_budget_s" in m for m in warnings), warnings


@pytest.mark.asyncio
async def test_transport_error_retries_respect_the_budget(monkeypatch):
    """The budget covers the exponential-backoff path too, not just
    Retry-After: a long backoff schedule stops once the sum won't fit."""
    sleeps = _sleep_recorder(monkeypatch)
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.TransportError("connection refused")

    # Schedule is [10, 20, 40, 80, ...]; 10 + 20 + 40 = 70 fits, the 80 does not.
    client = _client_with_handler(
        handler, max_retries=8, retry_backoff_s=10.0,
        retry_backoff_max_s=1000.0, retry_sleep_budget_s=100.0,
    )
    with pytest.raises(httpx.TransportError):
        await client.claim_next([TaskType.DETECT_CUT_PLANES], worker_id="w")
    await client.close()

    assert sleeps == [10.0, 20.0, 40.0]
    assert calls["n"] == 4


@pytest.mark.asyncio
async def test_budget_charges_measured_time_and_admission_only(monkeypatch):
    """Two halves of one contract, pinned together because each is only
    defensible next to the other.

    *Charged on the monotonic clock.* The worker's own task handler runs in
    this process; a handler that blocks the event loop makes
    ``asyncio.sleep(200)`` hand control back well after 200s. Summing requested
    delays would keep the arithmetic under budget while the polling loop is
    pinned for far longer — the exact stall the budget exists to prevent. Here
    every sleep costs twice its delay, so the budget is spent in half the
    attempts.

    *Admission, not a deadline.* Measuring narrows the gap but cannot close
    it: the second sleep is admitted on the remaining 200s and then costs 400s,
    so this 600s budget really spends 800s. Nothing running inside a blocked
    event loop can preempt that sleep — a ``wait_for`` timer is starved by the
    same block — so the overrun is bounded by the block, not by the budget.
    That is why the knob is named a budget and documented as one; a consumer
    picking a value must leave headroom for it.
    """
    import time

    clock = {"t": 1000.0}
    sleeps: list[float] = []

    async def overrunning_sleep(delay):
        sleeps.append(delay)
        clock["t"] += delay * 2  # event loop hands control back late

    monkeypatch.setattr(asyncio, "sleep", overrunning_sleep)
    monkeypatch.setattr(time, "monotonic", lambda: clock["t"])
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            429, headers={"Retry-After": "200"}, text="Too Many Requests",
        )

    client = _client_with_handler(
        handler, max_retries=6, retry_backoff_s=2.0, retry_sleep_budget_s=600.0,
    )
    with pytest.raises(httpx.HTTPStatusError):
        await client.claim_next([TaskType.DETECT_CUT_PLANES], worker_id="w")
    await client.close()

    # Two sleeps really cost 400 + 400 = 800s, so the third can't fit. Summing
    # requested delays would have allowed three (200 + 200 + 200 = 600) and
    # spent 1200s of wall clock inside a 600s budget.
    assert sleeps == [200.0, 200.0]
    assert calls["n"] == 3
    # The documented overshoot, asserted rather than left implicit: 800s spent
    # against a 600s budget. If a future change makes this a hard ceiling,
    # this assertion fails and the docstring/CHANGELOG wording must follow.
    assert clock["t"] - 1000.0 == 800.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "budget", [{}, {"retry_sleep_budget_s": None}], ids=["omitted", "explicit-none"],
)
async def test_no_budget_keeps_unbounded_retrying(monkeypatch, budget):
    """No budget — omitted (the default) or an explicit None — leaves retrying
    unbounded: the full attempt budget is spent on hour-long Retry-After
    delays, exactly as before this knob existed. This is what every consumer
    gets from the SDK bump alone, until it opts in."""
    sleeps = _sleep_recorder(monkeypatch)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429, headers={"Retry-After": "3600"}, text="Too Many Requests",
        )

    client = _client_with_handler(
        handler, max_retries=3, retry_backoff_s=2.0, **budget,
    )
    assert client.retry_sleep_budget_s is None
    with pytest.raises(httpx.HTTPStatusError):
        await client.claim_next([TaskType.DETECT_CUT_PLANES], worker_id="w")
    await client.close()

    assert sleeps == [3600.0, 3600.0]


@pytest.mark.asyncio
async def test_budget_does_not_disturb_normal_retry_schedules(monkeypatch):
    """A schedule that fits the budget is untouched — the recommended 600s is
    far above the ~14s the default retry config actually sleeps."""
    sleeps = _sleep_recorder(monkeypatch)
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 4:
            return httpx.Response(503, text="Service Unavailable")
        return httpx.Response(200)

    client = _client_with_handler(
        handler, max_retries=4, retry_backoff_s=2.0, retry_sleep_budget_s=600.0,
    )
    await client.complete(7, {"output": "done"})
    await client.close()

    assert sleeps == [2.0, 4.0, 8.0]
    assert calls["n"] == 4


def test_retry_knobs_are_keyword_only_on_both_public_constructors():
    """Adding a knob *between* existing retry parameters is only safe because
    every one of them is keyword-only on both public constructors.

    Were they positional-capable, inserting ``retry_sleep_budget_s`` ahead of
    ``retry_jitter`` would silently re-bind an existing caller's arguments —
    ``retry_jitter=True`` would land on the budget as a 1-second ceiling
    (``True == 1`` passes the ``> 0`` check), and everything after it would
    shift one slot. The ``*`` markers that rule this out predate the budget
    knob, so pin them here: the next knob inserted mid-list needs the same
    guarantee, and nothing else in the suite would fail if a ``*`` were
    dropped.
    """
    import inspect

    from task_worker_api.worker import Worker

    for ctor in (BackendClient.__init__, Worker.__init__):
        params = inspect.signature(ctor).parameters
        for name in (
            "max_retries",
            "retry_backoff_s",
            "retry_backoff_max_s",
            "retry_sleep_budget_s",
            "retry_jitter",
        ):
            assert params[name].kind is inspect.Parameter.KEYWORD_ONLY, (
                f"{ctor.__qualname__}: {name} must stay keyword-only so a "
                "later insertion can't re-bind a caller's arguments"
            )

    # The concrete call the ordering concern is about: nothing past the two
    # positional credentials binds positionally at all, on either constructor.
    with pytest.raises(TypeError):
        BackendClient("http://fake", "x", 4)
    with pytest.raises(TypeError):
        Worker("http://fake", "x", "w", {})
