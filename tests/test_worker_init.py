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
