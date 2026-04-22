"""Integration-style tests for Worker.run_one with FakeBackendClient.

Exercises the full claim → validate → handler → complete path without a
real HTTP backend. Covers happy path, schema rejection, handler failure,
and cooperative cancel.
"""
from __future__ import annotations

import pytest

from task_worker_api import (
    TaskCancelled,
    TaskContext,
    TaskType,
    Worker,
)
from task_worker_api.schemas import TASK_PARAMS_SCHEMAS, DetectCutPlanesParams
from task_worker_api.testing import FakeBackendClient


@pytest.mark.asyncio
async def test_worker_claims_runs_and_completes_happy_path(tmp_path):
    fake = FakeBackendClient()
    fake.queue_task(
        task_type=TaskType.DETECT_CUT_PLANES,
        params={
            # the STL doesn't need to exist: our handler returns a canned
            # payload without touching disk, and Worker.files puts it on
            # an empty input_dir when input_path points at a missing file.
            # So we avoid prepare_inputs by using a *real* tmp file.
        },
    )
    # Rewrite: add an actual input_path the prepare_inputs can read.
    fake._queue[0] = fake._queue[0].__class__(
        id=fake._queue[0].id,
        task_type=fake._queue[0].task_type,
        case_id=fake._queue[0].case_id,
        item_key=fake._queue[0].item_key,
        status=fake._queue[0].status,
        params={"input_path": str(tmp_path / "fake.stl"), "max_results": 3},
        worker_id=None,
    )
    (tmp_path / "fake.stl").write_bytes(b"solid empty\nendsolid empty\n")

    received: dict = {}

    async def handler(ctx: TaskContext, params: DetectCutPlanesParams) -> dict:
        received["max_results"] = params.max_results
        received["primary_name"] = ctx.files.primary_path.name
        return {"planes": [{"rank": 0}], "stats": {}}

    worker = Worker(
        backend_url="http://fake/api/v1",
        api_key="fake-key",
        worker_id="test-worker",
        handlers={TaskType.DETECT_CUT_PLANES: handler},
        work_dir=str(tmp_path / "work"),
        client=fake,
    )
    ran = await worker.run_one()
    assert ran is True

    assert received["max_results"] == 3
    assert received["primary_name"] == "fake.stl"
    assert len(fake.completed_tasks) == 1
    assert fake.completed_tasks[0]["result"] == {"planes": [{"rank": 0}], "stats": {}}
    assert fake.failed_tasks == []


@pytest.mark.asyncio
async def test_worker_rejects_params_with_extra_fields(tmp_path):
    """extra='forbid' on the schema → task fails with TaskParamsError."""
    fake = FakeBackendClient()
    (tmp_path / "fake.stl").write_bytes(b"solid\nendsolid\n")
    fake.queue_task(
        task_type=TaskType.DETECT_CUT_PLANES,
        params={
            "input_path": str(tmp_path / "fake.stl"),
            "input_file": "oops",  # spelled wrong — extra="forbid" rejects
        },
    )

    async def handler(ctx, params):  # should never be called
        raise AssertionError("handler must not run on invalid params")

    worker = Worker(
        backend_url="http://fake/api/v1", api_key="k", worker_id="w",
        handlers={TaskType.DETECT_CUT_PLANES: handler},
        work_dir=str(tmp_path / "work"), client=fake,
    )
    await worker.run_one()

    assert fake.completed_tasks == []
    assert len(fake.failed_tasks) == 1
    assert "failed schema validation" in fake.failed_tasks[0]["error"]


@pytest.mark.asyncio
async def test_worker_reports_handler_exception_as_fail(tmp_path):
    fake = FakeBackendClient()
    (tmp_path / "fake.stl").write_bytes(b"solid\nendsolid\n")
    fake.queue_task(
        task_type=TaskType.DETECT_CUT_PLANES,
        params={"input_path": str(tmp_path / "fake.stl")},
    )

    async def handler(ctx, params):
        raise RuntimeError("boom")

    worker = Worker(
        backend_url="http://fake/api/v1", api_key="k", worker_id="w",
        handlers={TaskType.DETECT_CUT_PLANES: handler},
        work_dir=str(tmp_path / "work"), client=fake,
    )
    await worker.run_one()

    assert fake.completed_tasks == []
    assert len(fake.failed_tasks) == 1
    assert "RuntimeError: boom" in fake.failed_tasks[0]["error"]


@pytest.mark.asyncio
async def test_worker_reports_cancel_as_fail_cooperative(tmp_path):
    fake = FakeBackendClient()
    (tmp_path / "fake.stl").write_bytes(b"solid\nendsolid\n")
    task = fake.queue_task(
        task_type=TaskType.DETECT_CUT_PLANES,
        params={"input_path": str(tmp_path / "fake.stl")},
    )
    fake.mark_cancelled(task.id)

    async def handler(ctx, params):
        # The handler never explicitly checks; the CancelGuard raises
        # TaskCancelled at the next await.
        import asyncio
        for _ in range(20):
            await asyncio.sleep(0.1)
        return {}  # pragma: no cover — should never reach

    worker = Worker(
        backend_url="http://fake/api/v1", api_key="k", worker_id="w",
        handlers={TaskType.DETECT_CUT_PLANES: handler},
        work_dir=str(tmp_path / "work"),
        client=fake,
        cancel_poll_interval_s=0.05,
    )
    await worker.run_one()

    assert fake.completed_tasks == []
    assert len(fake.failed_tasks) == 1
    assert "cancelled" in fake.failed_tasks[0]["error"].lower()


@pytest.mark.asyncio
async def test_task_type_without_handler_fails_fast(tmp_path):
    fake = FakeBackendClient()
    (tmp_path / "fake.stl").write_bytes(b"solid\nendsolid\n")
    fake.queue_task(
        task_type=TaskType.DETECT_CUT_PLANES,
        params={"input_path": str(tmp_path / "fake.stl")},
    )

    worker = Worker(
        backend_url="http://fake/api/v1", api_key="k", worker_id="w",
        handlers={},  # deliberately empty
        work_dir=str(tmp_path / "work"), client=fake,
    )
    # The worker will claim the task because its registered task_types is
    # empty, but claim_next is filtered by task_types... so no claim.
    # This asserts the negative: no task ran.
    ran = await worker.run_one()
    assert ran is False
    assert fake.completed_tasks == []
    assert fake.failed_tasks == []


def test_registry_contains_expected_types():
    assert TaskType.DETECT_CUT_PLANES in TASK_PARAMS_SCHEMAS
    assert TaskType.MODEL_INITIALIZING in TASK_PARAMS_SCHEMAS


def test_detect_cut_planes_schema_rejects_extra_fields():
    import pydantic
    with pytest.raises(pydantic.ValidationError):
        DetectCutPlanesParams(input_path="/tmp/x.stl", input_file="nope")
