"""Shared pytest fixtures for the task-worker-api test suite.

Centralises the test boilerplate that was duplicated across
test_worker_loop, test_worker_timeout, test_worker_init, and
test_payload_log_integration:

  - ``fake_client``        — a fresh ``FakeBackendClient`` per test
  - ``noop_handler``       — the async no-op handler used as a placeholder
  - ``detect_cut_planes_handlers`` — ``{DETECT_CUT_PLANES: noop_handler}``
  - ``make_worker``        — factory for ``Worker`` with standard fake config
  - ``queue_cut_planes_task`` — queues a task with a real tmp STL input file
"""
from __future__ import annotations

import pytest

from task_worker_api import TaskType, Worker
from task_worker_api.testing import FakeBackendClient

_FAKE_BACKEND_URL = "http://fake/api/v1"
_FAKE_API_KEY = "k"
_FAKE_WORKER_ID = "w"


@pytest.fixture
def fake_client() -> FakeBackendClient:
    """A fresh in-memory backend for each test."""
    return FakeBackendClient()


@pytest.fixture
def noop_handler():
    """Async handler that returns an empty dict — never actually invoked."""
    async def _handler(ctx, params):  # pragma: no cover — never invoked
        return {}
    return _handler


@pytest.fixture
def detect_cut_planes_handlers(noop_handler):
    """Single-handler map for DETECT_CUT_PLANES (the canonical test task type)."""
    return {TaskType.DETECT_CUT_PLANES: noop_handler}


@pytest.fixture
def make_worker(tmp_path, detect_cut_planes_handlers):
    """Factory for ``Worker`` instances wired to the fake backend.

    All tests share the same backend_url / api_key / worker_id / work_dir
    defaults; override any of them via kwargs.

        worker = make_worker(client=fake_client)
        worker = make_worker(handlers={...}, shared_volume_path=...)
    """
    def _make(
        *,
        client=None,
        handlers=None,
        backend_url=_FAKE_BACKEND_URL,
        api_key=_FAKE_API_KEY,
        worker_id=_FAKE_WORKER_ID,
        **kwargs,
    ) -> Worker:
        return Worker(
            backend_url=backend_url,
            api_key=api_key,
            worker_id=worker_id,
            handlers=(
                detect_cut_planes_handlers if handlers is None else handlers
            ),
            work_dir=str(tmp_path / "work"),
            client=client if client is not None else FakeBackendClient(),
            **kwargs,
        )
    return _make


@pytest.fixture
def queue_cut_planes_task(tmp_path, fake_client):
    """Queue a DETECT_CUT_PLANES task backed by a real (empty) STL on disk.

    Returns the ``ClaimedTask``. Additional params can be merged in.
    """
    def _queue(*, extra_params: dict | None = None) -> None:
        stl = tmp_path / "fake.stl"
        stl.write_bytes(b"solid\nendsolid\n")
        params: dict = {"input_path": str(stl)}
        if extra_params:
            params.update(extra_params)
        fake_client.queue_task(
            task_type=TaskType.DETECT_CUT_PLANES,
            params=params,
        )
    return _queue
