import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from task_worker_api.context import FileContext, TaskContext
from task_worker_api.enums import TaskType
from task_worker_api.errors import ProtocolError
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
@pytest.mark.parametrize("interrupt_staging, lease_cancelled, expected", [
    # Interrupted after start: the attempt is running and owes a verdict.
    (False, False, ["fail"]),
    # Expired lease: the token is dead, so no report can land.
    (False, True, []),
    # Interrupted while inputs stage, before start: the attempt is still
    # reserved, where the resolution is the supervisor's decline. A fail
    # journaled here is replayed by recovery, rejected, and aborts it before
    # that decline.
    (True, False, []),
])
async def test_interrupted_attempt_reports_a_terminal_fail_only_after_start(
        monkeypatch, tmp_path, interrupt_staging, lease_cancelled, expected):
    """A cancel is not an Exception: without a report a running attempt orphans."""
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

    async def stage(*args):
        if interrupt_staging:
            raise asyncio.CancelledError
        return files

    client = FakeBackendClient()
    client.resource_operation = AsyncMock()
    monkeypatch.delenv("SYNPUSHER_TARGETS", raising=False)
    monkeypatch.setattr("task_worker_api.resource_execution.AttemptLease", Lease)
    monkeypatch.setattr("task_worker_api.files.prepare_admitted_inputs", stage)
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


class _Lease:
    """Stand-in for AttemptLease: runs the handler, renews nothing."""

    def __init__(self, *args, **kwargs): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def start(self, report, digest): pass
    async def run(self, handler, ctx, params): return await handler(ctx, params)


def _oversized_attempt(tmp_path, result):
    """Worker + real journal/client wired for one admitted attempt returning ``result``."""
    from datetime import datetime, timedelta, timezone

    import httpx

    from task_worker_api.claim_journal import ClaimJournal
    from task_worker_api.client import BackendClient
    from .test_resources import cpu_profile as _profile

    task = AdmittedTask(id=1, task_type="model_initializing", case_id=None, item_key="mesh",
                        params=dict(job_id="job", input_path="mesh.stl", base_name="mesh"), inputs={})
    claim = _admitted_claim(uuid4()).model_copy(update={
        "task": task, "profile": _profile(profile_id="p", task_type=task.task_type),
        "input_digest": input_snapshot_digest(task), "state": "running"})
    journal = ClaimJournal(tmp_path / "claim.sqlite")
    request = journal.prepare(claim.ownership.worker_instance_id, frozenset({"model_initializing"}))
    journal.record_response(request.claim_request_id, claim)
    seen, bodies = [], []

    def handle(request):
        seen.append(request.url.path)
        bodies.append(request.content)
        if request.url.path == "/workers/ready":
            return httpx.Response(200, json={
                "worker_instance_id": str(claim.ownership.worker_instance_id), "ready": True})
        now = datetime.now(timezone.utc)
        return httpx.Response(200, json={
            "task_id": claim.task_id, "attempt_id": str(claim.ownership.attempt_id),
            "state": "released", "task_status": 3, "cancelled": False,
            "server_time": now.isoformat(),
            "lease_expires_at": (now + timedelta(seconds=60)).isoformat(),
            "execution_deadline": None, "progress": {},
        })

    transport = httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="http://test")
    backend = BackendClient("http://test", "test", client=transport)

    async def handler(ctx, params):
        return result

    return claim, journal, backend, transport, seen, bodies, handler


@pytest.mark.asyncio
async def test_oversized_result_fails_the_attempt_before_it_is_journaled(monkeypatch, tmp_path):
    """A result the wire cannot carry must never reach prepare_operation: the
    durable complete would be replayed forever against a 413 (not transient),
    leaving the attempt unresolved and the worker wedged behind
    previous_claim_unresolved. It becomes a terminal fail the supervisor can
    release instead."""
    claim, journal, backend, transport, seen, bodies, handler = _oversized_attempt(
        tmp_path, {"blob": "x" * 2_000_000})
    files = FileContext(tmp_path, tmp_path, tmp_path / "mesh.stl")

    monkeypatch.delenv("SYNPUSHER_TARGETS", raising=False)
    monkeypatch.setattr("task_worker_api.resource_execution.AttemptLease", _Lease)
    monkeypatch.setattr("task_worker_api.files.prepare_admitted_inputs", AsyncMock(return_value=files))
    monkeypatch.setattr("task_worker_api.worker._cuda_cleanup_with_timeout", AsyncMock(return_value=True))
    worker = Worker(backend_url="http://test", api_key="test", worker_id="test", client=backend,
                    work_dir=str(tmp_path), handlers={TaskType.MODEL_INITIALIZING: handler})

    with pytest.raises(ProtocolError, match="complete limit"):
        await worker.run_admitted_attempt(claim, journal, AsyncMock(return_value=None))

    # The terminal report is fail, and no complete was ever prepared or sent.
    assert seen == ["/tasks/1/fail"]
    assert journal.unresolved_operations() == []
    assert json.loads(bodies[0])["error"].startswith("ProtocolError: handler result serializes to")

    # Reconciliation completes: the release acknowledgement clears the journal,
    # so the supervisor's next readiness announcement is accepted.
    release = journal.prepare_operation("release", {}, request={})
    journal.record_operation("release", release, {
        "state": "released", "attempt_id": str(claim.ownership.attempt_id)})
    journal.acknowledge_release(claim.ownership.attempt_id)
    await backend.resource_ready(journal, claim.ownership.worker_instance_id)
    await transport.aclose()
    assert seen[-1] == "/workers/ready"


@pytest.mark.asyncio
async def test_a_normal_result_still_completes(monkeypatch, tmp_path):
    """The limit only rejects bodies the wire cannot carry."""
    claim, journal, backend, transport, seen, bodies, handler = _oversized_attempt(
        tmp_path, {"optimization_method": "blender"})
    files = FileContext(tmp_path, tmp_path, tmp_path / "mesh.stl")

    monkeypatch.delenv("SYNPUSHER_TARGETS", raising=False)
    monkeypatch.setattr("task_worker_api.resource_execution.AttemptLease", _Lease)
    monkeypatch.setattr("task_worker_api.files.prepare_admitted_inputs", AsyncMock(return_value=files))
    monkeypatch.setattr("task_worker_api.worker._cuda_cleanup_with_timeout", AsyncMock(return_value=True))
    worker = Worker(backend_url="http://test", api_key="test", worker_id="test", client=backend,
                    work_dir=str(tmp_path), handlers={TaskType.MODEL_INITIALIZING: handler})

    await worker.run_admitted_attempt(claim, journal, AsyncMock(return_value=None))
    await transport.aclose()
    assert seen == ["/tasks/1/complete"]
    assert journal.unresolved_operations() == []
