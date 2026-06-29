"""Direct tests for the FakeBackendClient test double.

FakeBackendClient is exercised indirectly through Worker.run_one tests, but
those only cover the happy path and the cancel path. This module gives
focused coverage of the fixture's own contract: claim ordering, type
filtering, progress/cancel semantics, and the async-context-manager surface.
"""
from __future__ import annotations

import pytest

from task_worker_api.enums import TaskStatus, TaskType
from task_worker_api.testing import FakeBackendClient


@pytest.mark.asyncio
async def test_claim_next_returns_none_on_empty_queue():
    fake = FakeBackendClient()
    result = await fake.claim_next([TaskType.DETECT_CUT_PLANES], worker_id="w")
    assert result is None


@pytest.mark.asyncio
async def test_claim_next_returns_tasks_in_fifo_order():
    fake = FakeBackendClient()
    t1 = fake.queue_task(task_type=TaskType.DETECT_CUT_PLANES, params={})
    t2 = fake.queue_task(task_type=TaskType.DETECT_CUT_PLANES, params={})
    claimed1 = await fake.claim_next([TaskType.DETECT_CUT_PLANES], worker_id="w")
    claimed2 = await fake.claim_next([TaskType.DETECT_CUT_PLANES], worker_id="w")
    assert claimed1 is not None and claimed1.id == t1.id
    assert claimed2 is not None and claimed2.id == t2.id
    assert await fake.claim_next([TaskType.DETECT_CUT_PLANES], worker_id="w") is None


@pytest.mark.asyncio
async def test_claim_next_filters_by_task_type():
    fake = FakeBackendClient()
    fake.queue_task(task_type=TaskType.GS_BUILD, params={})
    fake.queue_task(task_type=TaskType.DETECT_CUT_PLANES, params={})

    # Worker only handles DETECT_CUT_PLANES — the GS_BUILD task stays queued.
    claimed = await fake.claim_next(
        [TaskType.DETECT_CUT_PLANES], worker_id="w",
    )
    assert claimed is not None
    assert claimed.task_type == TaskType.DETECT_CUT_PLANES

    # The GS_BUILD task is still there.
    claimed_gs = await fake.claim_next([TaskType.GS_BUILD], worker_id="w")
    assert claimed_gs is not None
    assert claimed_gs.task_type == TaskType.GS_BUILD


@pytest.mark.asyncio
async def test_claim_next_accepts_string_task_types():
    """Workers may pass task types as raw strings (not enum members)."""
    fake = FakeBackendClient()
    fake.queue_task(task_type=TaskType.DETECT_CUT_PLANES, params={})
    claimed = await fake.claim_next(["detect_cut_planes"], worker_id="w")
    assert claimed is not None


@pytest.mark.asyncio
async def test_complete_captures_result():
    fake = FakeBackendClient()
    await fake.complete(42, {"planes": []})
    assert fake.completed_tasks == [{"task_id": 42, "result": {"planes": []}}]
    assert fake.failed_tasks == []


@pytest.mark.asyncio
async def test_fail_captures_error():
    fake = FakeBackendClient()
    await fake.fail(42, "boom")
    assert fake.failed_tasks == [{"task_id": 42, "error": "boom"}]
    assert fake.completed_tasks == []


@pytest.mark.asyncio
async def test_report_progress_records_event_and_returns_cancel_flag():
    fake = FakeBackendClient()
    fake.mark_cancelled(7)
    result = await fake.report_progress(
        7, stage="processing", current=3, total=10,
    )
    assert result == {"cancelled": True}
    assert fake.progress_events == [
        {"task_id": 7, "stage": "processing", "current": 3, "total": 10},
    ]


@pytest.mark.asyncio
async def test_report_progress_returns_not_cancelled_by_default():
    fake = FakeBackendClient()
    result = await fake.report_progress(1, stage="start")
    assert result == {"cancelled": False}


@pytest.mark.asyncio
async def test_get_cancel_status_reflects_mark_cancelled():
    fake = FakeBackendClient()
    # Before cancel
    status = await fake.get_cancel_status(5)
    assert status == {
        "cancelled": False,
        "status": int(TaskStatus.IN_PROGRESS),
        "cancelled_reason": None,
    }
    # After cancel
    fake.mark_cancelled(5)
    status = await fake.get_cancel_status(5)
    assert status == {
        "cancelled": True,
        "status": int(TaskStatus.CANCELLED),
        "cancelled_reason": "user",
    }


@pytest.mark.asyncio
async def test_queue_task_assigns_incrementing_ids():
    fake = FakeBackendClient()
    t1 = fake.queue_task(task_type=TaskType.DETECT_CUT_PLANES, params={})
    t2 = fake.queue_task(task_type=TaskType.DETECT_CUT_PLANES, params={})
    t3 = fake.queue_task(task_type=TaskType.DETECT_CUT_PLANES, params={})
    assert t1.id < t2.id < t3.id


@pytest.mark.asyncio
async def test_queue_task_preserves_case_id_and_item_key():
    fake = FakeBackendClient()
    task = fake.queue_task(
        task_type=TaskType.DETECT_CUT_PLANES,
        params={"input_path": "/tmp/x.stl"},
        case_id=99,
        item_key="case-99-v1",
    )
    assert task.case_id == 99
    assert task.item_key == "case-99-v1"
    assert task.params == {"input_path": "/tmp/x.stl"}


@pytest.mark.asyncio
async def test_download_file_raises_not_implemented(tmp_path):
    fake = FakeBackendClient()
    with pytest.raises(NotImplementedError):
        await fake.download_file(1, "input.stl", tmp_path / "out.stl")


@pytest.mark.asyncio
async def test_upload_file_raises_not_implemented(tmp_path):
    fake = FakeBackendClient()
    src = tmp_path / "out.stl"
    src.write_bytes(b"data")
    with pytest.raises(NotImplementedError):
        await fake.upload_file(1, "output.stl", src)


@pytest.mark.asyncio
async def test_async_context_manager_works():
    fake = FakeBackendClient()
    async with fake as ctx:
        assert ctx is fake
    # close() is a no-op; no exception raised.


@pytest.mark.asyncio
async def test_close_is_noop():
    fake = FakeBackendClient()
    await fake.close()  # must not raise
