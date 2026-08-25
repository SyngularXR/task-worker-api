"""Worker.__init__ fails fast on misconfiguration.

Covers three fail-fast guards that surface operator mistakes at boot rather
than at first claim (or worse, silently forever):

  1. A handler whose TaskType has no registered params schema would only
     blow up after claim_next pulls the first matching task off the queue,
     burning retry budget. The check gives feedback at deploy time.
  2. An empty handlers dict makes task_types=[] in the poll loop, so the
     worker silently polls forever without ever processing work.
  3. An externally-supplied client whose base_url disagrees with the
     worker's backend_url silently routes every request to the wrong
     endpoint — claim, complete, and file transfer all hit a host that
     doesn't know this worker_id.
"""
from __future__ import annotations

import pytest

from task_worker_api import TaskType, Worker
from task_worker_api.client import BackendClient
from task_worker_api.errors import ProtocolError


async def _noop_handler(ctx, params):  # pragma: no cover — never invoked
    return {}


def test_worker_init_rejects_handler_without_registered_schema(make_worker, noop_handler):
    """RENDER and APPLE_ML_GS are deferred per schemas/__init__.py — a Worker
    that registers a handler for one of them must fail at construction."""
    with pytest.raises(ProtocolError) as exc_info:
        make_worker(handlers={TaskType.RENDER: noop_handler})
    assert "no schema registered" in str(exc_info.value)
    assert TaskType.RENDER.value in str(exc_info.value)


def test_worker_init_accepts_handlers_with_registered_schemas(make_worker):
    """Sanity check: the happy path still constructs without raising."""
    make_worker()


def test_worker_init_rejects_empty_handlers(fake_client):
    """Empty handlers makes task_types=[] in the poll loop, so the worker
    silently polls forever without ever processing work — same shape of
    misconfiguration as a handler with no registered schema."""
    with pytest.raises(ProtocolError) as exc_info:
        Worker(
            backend_url="http://fake/api/v1",
            api_key="k",
            worker_id="w",
            handlers={},
            client=fake_client,
        )
    assert "handlers is empty" in str(exc_info.value)


# --- client/backend_url compatibility guard --------------------------------


async def test_worker_init_rejects_client_with_mismatched_base_url():
    """An external BackendClient whose base_url disagrees with backend_url
    would silently route every request to the wrong endpoint. The guard
    surfaces this at construction time instead."""
    mismatched = BackendClient("http://wrong-host:9999/api/v1", "k")
    try:
        with pytest.raises(ProtocolError) as exc_info:
            Worker(
                backend_url="http://fake/api/v1",
                api_key="k",
                worker_id="w",
                handlers={TaskType.DETECT_CUT_PLANES: _noop_handler},
                client=mismatched,
            )
        msg = str(exc_info.value)
        assert "compatible" in msg
        assert "http://fake/api/v1" in msg
        assert "http://wrong-host:9999/api/v1" in msg
    finally:
        await mismatched.close()


async def test_worker_init_accepts_client_with_matching_base_url():
    """A real BackendClient whose base_url equals backend_url constructs
    cleanly — the guard is purely additive for misconfigurations."""
    matched = BackendClient("http://fake/api/v1", "k")
    try:
        worker = Worker(
            backend_url="http://fake/api/v1",
            api_key="k",
            worker_id="w",
            handlers={TaskType.DETECT_CUT_PLANES: _noop_handler},
            client=matched,
        )
        assert worker.backend_url == "http://fake/api/v1"
    finally:
        await matched.close()


async def test_worker_init_accepts_client_base_url_prefix_of_backend_url():
    """The client's base_url may be a prefix of backend_url (or vice versa):
    a client targeting http://fake/api used with backend_url
    http://fake/api/v1 still hits the same host, so it's accepted."""
    prefix_client = BackendClient("http://fake/api", "k")
    try:
        Worker(
            backend_url="http://fake/api/v1",
            api_key="k",
            worker_id="w",
            handlers={TaskType.DETECT_CUT_PLANES: _noop_handler},
            client=prefix_client,
        )
    finally:
        await prefix_client.close()


async def test_worker_init_accepts_trailing_slash_mismatch():
    """A trailing slash on either side must not trip the guard —
    BackendClient normalises by rstrip('/'), and the guard does too."""
    slashy = BackendClient("http://fake/api/v1/", "k")
    try:
        Worker(
            backend_url="http://fake/api/v1",
            api_key="k",
            worker_id="w",
            handlers={TaskType.DETECT_CUT_PLANES: _noop_handler},
            client=slashy,
        )
    finally:
        await slashy.close()


def test_worker_init_skips_url_check_for_client_without_base_url(make_worker):
    """FakeBackendClient (and any test double) has no base_url attribute —
    it makes no real HTTP calls, so URL provenance is moot and the guard
    is skipped. This keeps the test double a true drop-in."""
    # make_worker passes a FakeBackendClient (no base_url) with
    # backend_url="http://fake/api/v1"; construction must succeed.
    worker = make_worker()
    assert worker.backend_url == "http://fake/api/v1"


async def test_worker_init_threads_file_timeout_to_client():
    """When Worker constructs its own BackendClient (no client= passed),
    file_timeout_s must be forwarded so large file transfers get the
    longer deadline rather than the 30s general request timeout."""
    worker = Worker(
        backend_url="http://fake/api/v1",
        api_key="k",
        worker_id="w",
        handlers={TaskType.DETECT_CUT_PLANES: _noop_handler},
        file_timeout_s=600.0,
    )
    try:
        assert worker._client._file_timeout is not None
        assert worker._client._file_timeout.read == 600.0
        assert worker._client._worker_params == {"worker_id": "w"}
    finally:
        await worker._client.close()


async def test_worker_init_default_file_timeout_is_300():
    """The default file_timeout_s (300s) must reach the BackendClient when
    the Worker constructs one itself — a strict improvement over the old
    single 30s timeout for every operation."""
    worker = Worker(
        backend_url="http://fake/api/v1",
        api_key="k",
        worker_id="w",
        handlers={TaskType.DETECT_CUT_PLANES: _noop_handler},
    )
    try:
        assert worker._client._file_timeout is not None
        assert worker._client._file_timeout.read == 300.0
    finally:
        await worker._client.close()


# --- cancel_timeout_s threading --------------------------------------------


async def test_worker_init_threads_cancel_timeout_to_client():
    """When Worker constructs its own BackendClient (no client= passed),
    cancel_timeout_s must be forwarded so the CancelGuard's cancel-poll
    gets the short deadline rather than the 30s general request timeout."""
    worker = Worker(
        backend_url="http://fake/api/v1",
        api_key="k",
        worker_id="w",
        handlers={TaskType.DETECT_CUT_PLANES: _noop_handler},
        cancel_timeout_s=3.0,
    )
    try:
        assert worker._client._cancel_timeout is not None
        assert worker._client._cancel_timeout.read == 3.0
    finally:
        await worker._client.close()


async def test_worker_init_default_cancel_timeout_is_5():
    """The default cancel_timeout_s (5s) must reach the BackendClient when
    the Worker constructs one itself — short enough that a stalled cancel-poll
    fails fast instead of blocking the CancelGuard for 30s."""
    worker = Worker(
        backend_url="http://fake/api/v1",
        api_key="k",
        worker_id="w",
        handlers={TaskType.DETECT_CUT_PLANES: _noop_handler},
    )
    try:
        assert worker._client._cancel_timeout is not None
        assert worker._client._cancel_timeout.read == 5.0
    finally:
        await worker._client.close()


# --- lifecycle_timeout_s threading -----------------------------------------


async def test_worker_init_threads_lifecycle_timeout_to_client():
    """When Worker constructs its own BackendClient (no client= passed),
    lifecycle_timeout_s must be forwarded so heartbeat/complete/fail calls
    get the shorter deadline rather than the 30s general request timeout."""
    worker = Worker(
        backend_url="http://fake/api/v1",
        api_key="k",
        worker_id="w",
        handlers={TaskType.DETECT_CUT_PLANES: _noop_handler},
        lifecycle_timeout_s=10.0,
    )
    try:
        assert worker._client._lifecycle_timeout is not None
        assert worker._client._lifecycle_timeout.read == 10.0
    finally:
        await worker._client.close()


async def test_worker_init_default_lifecycle_timeout_is_15():
    """The default lifecycle_timeout_s (15s) must reach the BackendClient
    when the Worker constructs one itself — short enough that a stalled
    heartbeat/complete/fail fails fast instead of blocking the polling
    loop for 30s × max_retries (~120s)."""
    worker = Worker(
        backend_url="http://fake/api/v1",
        api_key="k",
        worker_id="w",
        handlers={TaskType.DETECT_CUT_PLANES: _noop_handler},
    )
    try:
        assert worker._client._lifecycle_timeout is not None
        assert worker._client._lifecycle_timeout.read == 15.0
    finally:
        await worker._client.close()


# --- retry-tuning parameter threading --------------------------------------


async def test_worker_init_threads_retry_params_to_client():
    """When Worker constructs its own BackendClient (no client= passed),
    the retry-tuning parameters (max_retries, retry_backoff_s,
    retry_backoff_max_s, retry_sleep_budget_s, retry_jitter) must be forwarded
    so operators who use the simple Worker(...) constructor can tune retry
    behaviour per workload without manually constructing a BackendClient."""
    worker = Worker(
        backend_url="http://fake/api/v1",
        api_key="k",
        worker_id="w",
        handlers={TaskType.DETECT_CUT_PLANES: _noop_handler},
        max_retries=8,
        retry_backoff_s=1.5,
        retry_backoff_max_s=120.0,
        retry_sleep_budget_s=900.0,
        retry_jitter=False,
    )
    try:
        assert worker._client.max_retries == 8
        assert worker._client.retry_backoff_s == 1.5
        assert worker._client.retry_backoff_max_s == 120.0
        assert worker._client.retry_sleep_budget_s == 900.0
        assert worker._client.retry_jitter is False
    finally:
        await worker._client.close()


async def test_worker_init_default_retry_params_match_client_defaults():
    """The default retry policy (4 attempts / 2s base / 60s cap / no total
    sleep budget / jitter on) must reach the BackendClient when the Worker
    constructs one itself — identical to what a directly-constructed
    BackendClient gets, so upgrading to Worker(...) changes nothing about
    retry behaviour."""
    worker = Worker(
        backend_url="http://fake/api/v1",
        api_key="k",
        worker_id="w",
        handlers={TaskType.DETECT_CUT_PLANES: _noop_handler},
    )
    try:
        assert worker._client.max_retries == 4
        assert worker._client.retry_backoff_s == 2.0
        assert worker._client.retry_backoff_max_s == 60.0
        assert worker._client.retry_sleep_budget_s is None
        assert worker._client.retry_jitter is True
    finally:
        await worker._client.close()


async def test_worker_init_retry_backoff_max_none_disables_cap():
    """Passing retry_backoff_max_s=None must propagate to the client as None
    (disabling the per-delay cap), matching BackendClient's documented
    'pass None to disable the cap' contract."""
    worker = Worker(
        backend_url="http://fake/api/v1",
        api_key="k",
        worker_id="w",
        handlers={TaskType.DETECT_CUT_PLANES: _noop_handler},
        retry_backoff_max_s=None,
    )
    try:
        assert worker._client.retry_backoff_max_s is None
    finally:
        await worker._client.close()


def test_worker_init_ignores_retry_params_when_client_supplied(make_worker, fake_client):
    """When an external client is supplied, retry-tuning parameters are
    irrelevant — the client is used as-is and the SDK doesn't reach in.
    Passing them must not raise (they're simply ignored), preserving the
    escape-hatch contract that an externally-supplied client is untouched."""
    # max_retries etc. are accepted by the signature but never applied to
    # the FakeBackendClient; construction must succeed.
    worker = make_worker(
        client=fake_client,
        max_retries=2,
        retry_backoff_s=0.5,
        retry_jitter=False,
    )
    # FakeBackendClient has no retry attributes; the point is no crash.
    assert worker._client is fake_client


# --- heartbeat_warn_threshold threading ------------------------------------


async def test_worker_init_threads_heartbeat_warn_threshold():
    """When Worker constructs its own BackendClient (no client= passed),
    heartbeat_warn_threshold must be forwarded to the ProgressReporter it
    builds per task so operators can tune the consecutive-failure count
    that escalates heartbeat logging from DEBUG to WARNING."""
    worker = Worker(
        backend_url="http://fake/api/v1",
        api_key="k",
        worker_id="w",
        handlers={TaskType.DETECT_CUT_PLANES: _noop_handler},
        heartbeat_warn_threshold=5,
    )
    try:
        assert worker.heartbeat_warn_threshold == 5
    finally:
        await worker._client.close()


async def test_worker_init_default_heartbeat_warn_threshold_is_3():
    """The default heartbeat_warn_threshold (3) means a long-running
    worker tolerates a couple of transient heartbeat blips at DEBUG before
    escalating to WARNING — loud enough to catch a sustained backend
    outage, quiet enough not to spam on a single restart."""
    worker = Worker(
        backend_url="http://fake/api/v1",
        api_key="k",
        worker_id="w",
        handlers={TaskType.DETECT_CUT_PLANES: _noop_handler},
    )
    try:
        assert worker.heartbeat_warn_threshold == 3
    finally:
        await worker._client.close()


async def test_worker_init_rejects_max_retries_below_one():
    """max_retries < 1 makes the BackendClient retry loop never execute,
    crashing the worker with an opaque AssertionError. Worker must surface
    the same ValueError at construction that BackendClient does."""
    with pytest.raises(ValueError, match="max_retries must be >= 1"):
        Worker(
            backend_url="http://fake/api/v1",
            api_key="k",
            worker_id="w",
            handlers={TaskType.DETECT_CUT_PLANES: _noop_handler},
            max_retries=0,
        )
