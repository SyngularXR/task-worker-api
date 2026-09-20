import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from task_worker_api.context import FileContext, TaskContext
from task_worker_api.enums import TaskType
from task_worker_api.resources import AdmittedTask, input_snapshot_digest
from task_worker_api.testing import FakeBackendClient
from task_worker_api.worker import Worker
from .test_resources import _admitted_claim, cpu_profile


@pytest.mark.asyncio
async def test_handler_receives_granted_profile_without_changing_payload(monkeypatch, tmp_path):
    task = AdmittedTask(id=1, task_type="model_initializing", case_id=None, item_key="mesh",
                        params=dict(job_id="job", input_path="mesh.stl", base_name="mesh"), inputs={})
    profile = cpu_profile(profile_id="model-initializing-blender-120k", task_type=task.task_type)
    claim = _admitted_claim(uuid4()).model_copy(update={
        "task": task, "profile": profile, "input_digest": input_snapshot_digest(task), "state": "reserved"})
    original = claim.model_dump_json()
    journal = SimpleNamespace(pending=lambda: ("claim", {"claim": claim.model_dump(mode="json")}))
    files = FileContext(tmp_path, tmp_path, tmp_path / "mesh.stl")
    assert TaskContext(None, files, None).profile is None

    class Lease:
        def __init__(self, *args, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def start(self, report, digest): assert digest == claim.input_digest
        async def run(self, handler, ctx, params): return await handler(ctx, params)

    async def handler(ctx, params):
        assert ctx.profile is profile
        assert ctx.task.params == task.params
        return {"optimization_method": "blender"}

    client = FakeBackendClient()
    client.resource_operation = AsyncMock()
    monkeypatch.delenv("SYNPUSHER_TARGETS", raising=False)
    monkeypatch.setattr("task_worker_api.resource_execution.AttemptLease", Lease)
    monkeypatch.setattr("task_worker_api.files.prepare_admitted_inputs", AsyncMock(return_value=files))
    monkeypatch.setattr("task_worker_api.worker._cuda_cleanup_with_timeout", AsyncMock(return_value=True))
    worker = Worker(backend_url="http://test", api_key="test", worker_id="test", client=client,
                    work_dir=str(tmp_path), handlers={TaskType.MODEL_INITIALIZING: handler})
    await worker.run_admitted_attempt(claim, journal, AsyncMock(return_value=None))
    assert client.resource_operation.call_args.args[1] == "complete"
    assert claim.model_dump_json() == original
    assert input_snapshot_digest(claim.task) == claim.input_digest


@pytest.mark.asyncio
async def test_publication_validation_does_not_block_the_event_loop(monkeypatch, tmp_path):
    """A stalled output walk must not freeze the loop the lease heartbeat runs on."""
    task = AdmittedTask(id=1, task_type="model_initializing", case_id=None, item_key="mesh",
                        params=dict(job_id="job", input_path="mesh.stl", base_name="mesh"), inputs={})
    claim = _admitted_claim(uuid4()).model_copy(update={
        "task": task, "profile": cpu_profile(profile_id="p", task_type=task.task_type),
        "input_digest": input_snapshot_digest(task), "state": "reserved"})
    journal = SimpleNamespace(pending=lambda: ("claim", {"claim": claim.model_dump(mode="json")}))
    files = FileContext(tmp_path, tmp_path, tmp_path / "mesh.stl")
    ticked = threading.Event()
    observed = []

    def blocking_sources(output_dir, names):
        # Runs off-loop iff the callback the handler queued gets a turn.
        observed.append(ticked.wait(5))
        return {}

    class Lease:
        def __init__(self, *args, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def start(self, report, digest): pass
        async def run(self, handler, ctx, params): return await handler(ctx, params)

    async def handler(ctx, params):
        asyncio.get_running_loop().call_soon(ticked.set)
        return {"output_files": {"mesh": "mesh.glb"}}

    client = FakeBackendClient()
    client.resource_operation = AsyncMock()
    monkeypatch.delenv("SYNPUSHER_TARGETS", raising=False)
    monkeypatch.setattr("task_worker_api.resource_execution.AttemptLease", Lease)
    monkeypatch.setattr("task_worker_api.files.prepare_admitted_inputs", AsyncMock(return_value=files))
    monkeypatch.setattr("task_worker_api.files._require_output_sources", blocking_sources)
    monkeypatch.setattr("task_worker_api.worker._cuda_cleanup_with_timeout", AsyncMock(return_value=True))
    worker = Worker(backend_url="http://test", api_key="test", worker_id="test", client=client,
                    work_dir=str(tmp_path), handlers={TaskType.MODEL_INITIALIZING: handler})
    await worker.run_admitted_attempt(claim, journal, AsyncMock(return_value=None))
    assert observed == [True]
    assert client.resource_operation.call_args.args[1] == "complete"


@pytest.mark.asyncio
@pytest.mark.parametrize("lease_cancelled, expected", [(False, ["fail"]), (True, [])])
async def test_interrupted_attempt_reports_a_terminal_fail(monkeypatch, tmp_path,
                                                           lease_cancelled, expected):
    """A cancel is not an Exception: without a report the attempt orphans running."""
    task = AdmittedTask(id=1, task_type="model_initializing", case_id=None, item_key="mesh",
                        params=dict(job_id="job", input_path="mesh.stl", base_name="mesh"), inputs={})
    claim = _admitted_claim(uuid4()).model_copy(update={
        "task": task, "profile": cpu_profile(profile_id="p", task_type=task.task_type),
        "input_digest": input_snapshot_digest(task), "state": "reserved"})
    journal = SimpleNamespace(pending=lambda: ("claim", {"claim": claim.model_dump(mode="json")}))
    files = FileContext(tmp_path, tmp_path, tmp_path / "mesh.stl")

    class Lease:
        def __init__(self, *args, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def start(self, report, digest): pass
        async def run(self, handler, ctx, params): return await handler(ctx, params)
        is_cancelled = lease_cancelled

    async def handler(ctx, params):
        raise asyncio.CancelledError

    client = FakeBackendClient()
    client.resource_operation = AsyncMock()
    monkeypatch.delenv("SYNPUSHER_TARGETS", raising=False)
    monkeypatch.setattr("task_worker_api.resource_execution.AttemptLease", Lease)
    monkeypatch.setattr("task_worker_api.files.prepare_admitted_inputs", AsyncMock(return_value=files))
    monkeypatch.setattr("task_worker_api.worker._cuda_cleanup_with_timeout", AsyncMock(return_value=True))
    worker = Worker(backend_url="http://test", api_key="test", worker_id="test", client=client,
                    work_dir=str(tmp_path), handlers={TaskType.MODEL_INITIALIZING: handler})
    with pytest.raises(asyncio.CancelledError):
        await worker.run_admitted_attempt(claim, journal, AsyncMock(return_value=None))
    assert [c.args[1] for c in client.resource_operation.call_args_list] == expected
    if expected:
        payload = client.resource_operation.call_args.args[2]
        assert "interrupted" in payload["error"] and payload["failure_kind"] == "error"


@pytest.mark.asyncio
async def test_a_failing_interrupt_report_never_masks_the_cancel(monkeypatch, tmp_path):
    task = AdmittedTask(id=1, task_type="model_initializing", case_id=None, item_key="mesh",
                        params=dict(job_id="job", input_path="mesh.stl", base_name="mesh"), inputs={})
    claim = _admitted_claim(uuid4()).model_copy(update={
        "task": task, "profile": cpu_profile(profile_id="p", task_type=task.task_type),
        "input_digest": input_snapshot_digest(task), "state": "reserved"})
    journal = SimpleNamespace(pending=lambda: ("claim", {"claim": claim.model_dump(mode="json")}))
    files = FileContext(tmp_path, tmp_path, tmp_path / "mesh.stl")

    class Lease:
        def __init__(self, *args, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def start(self, report, digest): pass
        async def run(self, handler, ctx, params): return await handler(ctx, params)
        is_cancelled = False

    async def handler(ctx, params):
        raise asyncio.CancelledError

    client = FakeBackendClient()
    client.resource_operation = AsyncMock(side_effect=RuntimeError("backend down"))
    monkeypatch.delenv("SYNPUSHER_TARGETS", raising=False)
    monkeypatch.setattr("task_worker_api.resource_execution.AttemptLease", Lease)
    monkeypatch.setattr("task_worker_api.files.prepare_admitted_inputs", AsyncMock(return_value=files))
    monkeypatch.setattr("task_worker_api.worker._cuda_cleanup_with_timeout", AsyncMock(return_value=True))
    worker = Worker(backend_url="http://test", api_key="test", worker_id="test", client=client,
                    work_dir=str(tmp_path), handlers={TaskType.MODEL_INITIALIZING: handler})
    with pytest.raises(asyncio.CancelledError):
        await worker.run_admitted_attempt(claim, journal, AsyncMock(return_value=None))
