import asyncio
import contextlib
import sys
import threading
import types

import pytest

from task_worker_api.errors import ProtocolError
from task_worker_api.worker import _cuda_cleanup_with_timeout


@pytest.mark.asyncio
@pytest.mark.parametrize("allocated,reserved,expected", [(0, 0, True), (1, 0, False), (0, 1, False)])
async def test_cleanup_distinguishes_live_storage(monkeypatch, allocated, reserved, expected):
    calls = []
    cuda = types.SimpleNamespace(
        is_initialized=lambda: True, device_count=lambda: 1,
        device=lambda index: contextlib.nullcontext(),
        synchronize=lambda index: calls.append("sync"),
        empty_cache=lambda: calls.append("empty"),
        memory_allocated=lambda index: allocated, memory_reserved=lambda index: reserved,
    )
    monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(cuda=cuda,
        _C=types.SimpleNamespace(_cuda_clearCublasWorkspaces=lambda: calls.append("workspaces"))))
    assert await _cuda_cleanup_with_timeout(2) is expected
    assert calls == ["sync", "workspaces", "empty"]


@pytest.mark.asyncio
async def test_wedged_cuda_has_a_deadline_and_does_not_block_event_loop(monkeypatch):
    release = threading.Event()
    entered = threading.Event()

    def wedged():
        entered.set()
        release.wait()
        return False

    monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(cuda=types.SimpleNamespace(is_initialized=wedged)))
    try:
        cleanup = asyncio.create_task(_cuda_cleanup_with_timeout(0.1))
        await asyncio.sleep(0.02)
        assert entered.is_set() and not cleanup.done()
        assert await asyncio.wait_for(cleanup, 1) is False
    finally:
        release.set()


@pytest.mark.asyncio
async def test_worker_cleanup_failure_blocks_next_claim(monkeypatch, make_worker, fake_client, queue_cut_planes_task):
    first = queue_cut_planes_task()
    second = queue_cut_planes_task()
    exits = []

    async def failed_cleanup(timeout):
        assert fake_client.completed_tasks[0]["task_id"] == first.id
        return False

    monkeypatch.setattr("task_worker_api.worker._cuda_cleanup_with_timeout", failed_cleanup)
    worker = make_worker(client=fake_client, on_hard_exit=lambda: exits.append(True))
    with pytest.raises(ProtocolError, match="cleanup unverified"):
        await worker.run_one()
    with pytest.raises(ProtocolError, match="cleanup unverified"):
        await worker.run_one()
    assert exits == [True]
    assert fake_client._queue[0].id == second.id
