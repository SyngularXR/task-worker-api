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


# ----- Worker payload-logger wiring -----------------------------------------

@pytest.mark.asyncio
async def test_worker_constructs_payload_logger_when_shared_volume_set(tmp_path):
    fake = FakeBackendClient()
    worker = Worker(
        backend_url="http://fake/api/v1", api_key="k", worker_id="w",
        handlers={}, work_dir=str(tmp_path / "work"), client=fake,
        shared_volume_path=str(tmp_path / "shared"),
    )
    assert worker._payload_logger is not None
    assert worker._payload_logger.enabled is True
    assert (tmp_path / "shared" / "_worker_payloads" / "w").is_dir()


@pytest.mark.asyncio
async def test_worker_disabled_when_shared_volume_unset(tmp_path):
    """Existing tests rely on this — no shared_volume_path means no logger."""
    fake = FakeBackendClient()
    worker = Worker(
        backend_url="http://fake/api/v1", api_key="k", worker_id="w",
        handlers={}, work_dir=str(tmp_path / "work"), client=fake,
    )
    assert worker._payload_logger is not None
    assert worker._payload_logger.enabled is False


@pytest.mark.asyncio
async def test_worker_disabled_via_env_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKER_PAYLOAD_LOG_ENABLED", "false")
    fake = FakeBackendClient()
    worker = Worker(
        backend_url="http://fake/api/v1", api_key="k", worker_id="w",
        handlers={}, work_dir=str(tmp_path / "work"), client=fake,
        shared_volume_path=str(tmp_path / "shared"),
    )
    assert worker._payload_logger.enabled is False


@pytest.mark.asyncio
async def test_worker_retention_env_falls_back_on_bad_value(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("WORKER_PAYLOAD_LOG_RETENTION_DAYS", "abc")
    fake = FakeBackendClient()
    with caplog.at_level("WARNING"):
        worker = Worker(
            backend_url="http://fake/api/v1", api_key="k", worker_id="w",
            handlers={}, work_dir=str(tmp_path / "work"), client=fake,
            shared_volume_path=str(tmp_path / "shared"),
        )
    assert worker._payload_logger.retention_days == 14
    assert any("retention" in r.message.lower() for r in caplog.records)


@pytest.mark.asyncio
async def test_worker_retention_env_falls_back_on_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKER_PAYLOAD_LOG_RETENTION_DAYS", "0")
    fake = FakeBackendClient()
    worker = Worker(
        backend_url="http://fake/api/v1", api_key="k", worker_id="w",
        handlers={}, work_dir=str(tmp_path / "work"), client=fake,
        shared_volume_path=str(tmp_path / "shared"),
    )
    assert worker._payload_logger.retention_days == 14


@pytest.mark.asyncio
async def test_worker_sanitizes_worker_id_in_log_path(tmp_path):
    """worker_id with slashes/.. must not escape into a sibling directory."""
    fake = FakeBackendClient()
    Worker(
        backend_url="http://fake/api/v1", api_key="k",
        worker_id="../etc/passwd",
        handlers={}, work_dir=str(tmp_path / "work"), client=fake,
        shared_volume_path=str(tmp_path / "shared"),
    )
    children = list((tmp_path / "shared" / "_worker_payloads").iterdir())
    assert len(children) == 1
    sanitized = children[0].name
    # Path separators are what would actually escape into a sibling dir;
    # `.` characters are allowed (the dir is a single segment).
    assert "/" not in sanitized
    assert "\\" not in sanitized
    assert "passwd" in sanitized
    # The directory must be a direct child of _worker_payloads.
    assert children[0].parent.name == "_worker_payloads"


# ----- _run_one capture -----------------------------------------------------

import json


@pytest.mark.asyncio
async def test_worker_writes_typed_record_on_happy_path(tmp_path):
    fake = FakeBackendClient()
    (tmp_path / "fake.stl").write_bytes(b"solid\nendsolid\n")
    fake.queue_task(
        task_type=TaskType.DETECT_CUT_PLANES,
        params={"input_path": str(tmp_path / "fake.stl"), "max_results": 3},
    )

    async def handler(ctx, params):
        return {"planes": []}

    shared = tmp_path / "shared"
    worker = Worker(
        backend_url="http://fake/api/v1", api_key="k", worker_id="w",
        handlers={TaskType.DETECT_CUT_PLANES: handler},
        work_dir=str(tmp_path / "work"), client=fake,
        shared_volume_path=str(shared),
    )
    await worker.run_one()
    worker._payload_logger.close()

    payload_dir = shared / "_worker_payloads" / "w"
    files = list(payload_dir.glob("payloads-*.jsonl"))
    assert len(files) == 1
    entry = json.loads(files[0].read_text(encoding="utf-8").strip())
    assert entry["task_type"] == "detect_cut_planes"
    assert entry["params"]["max_results"] == 3


@pytest.mark.asyncio
async def test_worker_writes_typed_record_even_on_schema_rejection(tmp_path):
    """Malformed payloads are exactly the bugs we most want to replay."""
    fake = FakeBackendClient()
    (tmp_path / "fake.stl").write_bytes(b"solid\nendsolid\n")
    fake.queue_task(
        task_type=TaskType.DETECT_CUT_PLANES,
        params={"input_path": str(tmp_path / "fake.stl"), "input_file": "oops"},
    )

    async def handler(ctx, params):
        raise AssertionError("must not run")

    shared = tmp_path / "shared"
    worker = Worker(
        backend_url="http://fake/api/v1", api_key="k", worker_id="w",
        handlers={TaskType.DETECT_CUT_PLANES: handler},
        work_dir=str(tmp_path / "work"), client=fake,
        shared_volume_path=str(shared),
    )
    await worker.run_one()
    worker._payload_logger.close()

    files = list((shared / "_worker_payloads" / "w").glob("payloads-*.jsonl"))
    entry = json.loads(files[0].read_text(encoding="utf-8").strip())
    # Captured BEFORE schema validation, so the bad field is preserved.
    assert entry["params"]["input_file"] == "oops"
    # And the task itself was failed:
    assert len(fake.failed_tasks) == 1
