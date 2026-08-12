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
async def test_poll_cancel_status_reflects_mark_cancelled():
    """poll_cancel_status mirrors BackendClient.poll_cancel_status on the
    fake — same data as get_cancel_status (the fake doesn't simulate HTTP
    retries)."""
    fake = FakeBackendClient()
    # Before cancel
    status = await fake.poll_cancel_status(5)
    assert status == {
        "cancelled": False,
        "status": int(TaskStatus.IN_PROGRESS),
        "cancelled_reason": None,
    }
    # After cancel
    fake.mark_cancelled(5)
    status = await fake.poll_cancel_status(5)
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
async def test_download_file_writes_staged_content(tmp_path):
    fake = FakeBackendClient()
    fake.queue_file(1, "input.stl", b"stl-bytes")
    dest = tmp_path / "out.stl"
    await fake.download_file(1, "input.stl", dest)
    assert dest.read_bytes() == b"stl-bytes"


@pytest.mark.asyncio
async def test_download_file_creates_parent_dirs(tmp_path):
    """prepare_inputs writes into work_dir/in/<filename>; dest may not exist yet."""
    fake = FakeBackendClient()
    fake.queue_file(1, "mesh.ply", b"ply-data")
    dest = tmp_path / "in" / "mesh.ply"
    await fake.download_file(1, "mesh.ply", dest)
    assert dest.read_bytes() == b"ply-data"


@pytest.mark.asyncio
async def test_download_file_accepts_str_content(tmp_path):
    fake = FakeBackendClient()
    fake.queue_file(1, "notes.txt", "hello world")
    dest = tmp_path / "notes.txt"
    await fake.download_file(1, "notes.txt", dest)
    assert dest.read_bytes() == b"hello world"


@pytest.mark.asyncio
async def test_download_file_raises_file_not_found_for_unstaged(tmp_path):
    fake = FakeBackendClient()
    with pytest.raises(FileNotFoundError):
        await fake.download_file(1, "missing.stl", tmp_path / "out.stl")


@pytest.mark.asyncio
async def test_download_file_scoped_per_task(tmp_path):
    """The same filename staged for task 1 must not be served for task 2."""
    fake = FakeBackendClient()
    fake.queue_file(1, "input.stl", b"task-one")
    dest = tmp_path / "out.stl"
    await fake.download_file(1, "input.stl", dest)
    assert dest.read_bytes() == b"task-one"
    with pytest.raises(FileNotFoundError):
        await fake.download_file(2, "input.stl", dest)


@pytest.mark.asyncio
async def test_download_file_honours_cancel_event(tmp_path):
    """The fake must mirror ``BackendClient.download_file``'s cancel
    contract: a set event raises TaskCancelled and writes nothing. Worker
    tests drive cancels through the fake, so a fake that ignored the event
    would report green while the real client aborts."""
    import asyncio

    from task_worker_api.errors import TaskCancelled

    fake = FakeBackendClient()
    fake.queue_file(1, "input.stl", b"stl-bytes")
    dest = tmp_path / "out.stl"
    cancelled = asyncio.Event()
    cancelled.set()

    with pytest.raises(TaskCancelled):
        await fake.download_file(1, "input.stl", dest, cancelled=cancelled)
    assert not dest.exists()


@pytest.mark.asyncio
async def test_download_file_unset_cancel_event_writes_content(tmp_path):
    """An event that never fires must not perturb the download."""
    import asyncio

    fake = FakeBackendClient()
    fake.queue_file(1, "input.stl", b"stl-bytes")
    dest = tmp_path / "out.stl"
    await fake.download_file(1, "input.stl", dest, cancelled=asyncio.Event())
    assert dest.read_bytes() == b"stl-bytes"


@pytest.mark.asyncio
async def test_upload_file_captures_bytes(tmp_path):
    fake = FakeBackendClient()
    src = tmp_path / "output.stl"
    src.write_bytes(b"result-bytes")
    await fake.upload_file(1, "output.stl", src)
    assert fake.uploaded_files == {(1, "output.stl"): b"result-bytes"}


@pytest.mark.asyncio
async def test_upload_file_overwrites_on_resend(tmp_path):
    fake = FakeBackendClient()
    src = tmp_path / "output.stl"
    src.write_bytes(b"v1")
    await fake.upload_file(1, "output.stl", src)
    src.write_bytes(b"v2")
    await fake.upload_file(1, "output.stl", src)
    assert fake.uploaded_files[(1, "output.stl")] == b"v2"


@pytest.mark.asyncio
async def test_upload_file_scoped_per_task_and_filename(tmp_path):
    fake = FakeBackendClient()
    src_a = tmp_path / "a.stl"
    src_a.write_bytes(b"aaa")
    src_b = tmp_path / "b.stl"
    src_b.write_bytes(b"bbb")
    await fake.upload_file(1, "a.stl", src_a)
    await fake.upload_file(2, "b.stl", src_b)
    assert fake.uploaded_files == {
        (1, "a.stl"): b"aaa",
        (2, "b.stl"): b"bbb",
    }


@pytest.mark.asyncio
async def test_roundtrip_download_then_upload(tmp_path):
    """Exercises the full prepare_inputs→handler→upload_outputs remote path."""
    fake = FakeBackendClient()
    fake.queue_file(7, "scene.ply", b"scene-bytes")
    downloaded = tmp_path / "scene.ply"
    await fake.download_file(7, "scene.ply", downloaded)
    assert downloaded.read_bytes() == b"scene-bytes"

    produced = tmp_path / "preview.png"
    produced.write_bytes(b"png-bytes")
    await fake.upload_file(7, "preview.png", produced)
    assert fake.uploaded_files[(7, "preview.png")] == b"png-bytes"


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
