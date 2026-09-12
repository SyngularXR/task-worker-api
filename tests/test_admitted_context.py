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
