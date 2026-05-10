"""Worker.__init__ fails fast when a handler has no registered schema.

Without this guard, a misconfigured Worker constructs cleanly and only
raises ProtocolError after claim_next returns the first matching task —
by which point the worker has already pulled work off the queue and
will burn retry budget marking it failed. The check at construction
time gives operators feedback at deploy/boot rather than at first claim.
"""
from __future__ import annotations

import pytest

from task_worker_api import TaskType, Worker
from task_worker_api.errors import ProtocolError
from task_worker_api.testing import FakeBackendClient


async def _noop_handler(ctx, params):  # pragma: no cover — never invoked
    return {}


def test_worker_init_rejects_handler_without_registered_schema():
    """RENDER and APPLE_ML_GS are deferred per schemas/__init__.py — a Worker
    that registers a handler for one of them must fail at construction."""
    with pytest.raises(ProtocolError) as exc_info:
        Worker(
            backend_url="http://fake/api/v1",
            api_key="k",
            worker_id="w",
            handlers={TaskType.RENDER: _noop_handler},
            client=FakeBackendClient(),
        )
    assert "no schema registered" in str(exc_info.value)
    assert TaskType.RENDER.value in str(exc_info.value)


def test_worker_init_accepts_handlers_with_registered_schemas():
    """Sanity check: the happy path still constructs without raising."""
    Worker(
        backend_url="http://fake/api/v1",
        api_key="k",
        worker_id="w",
        handlers={TaskType.DETECT_CUT_PLANES: _noop_handler},
        client=FakeBackendClient(),
    )
