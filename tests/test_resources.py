"""Trust-boundary resource validation, without inventing production budgets."""
import pytest
import asyncio
from datetime import datetime, timezone
from uuid import uuid4
import httpx
from pydantic import ValidationError

from task_worker_api.claim_journal import ClaimJournal
from task_worker_api.client import BackendClient
from task_worker_api.resources import AdmissionError
from task_worker_api.resources import Capacity, ClaimRequest, ResourceProfile, HostSnapshot
from task_worker_api.resource_protocol import SignedHostReport


@pytest.mark.parametrize("path", ["../escape", "/absolute", "a//b", "a\\b", "C:/file", "a/NUL"])
def test_input_layout_rejects_escaping_or_ambiguous_paths(path):
    from task_worker_api.resources import InputArtifact

    with pytest.raises(ValidationError):
        InputArtifact(filename="input.bin", path=path, sha256="a" * 64, size_bytes=1)


def cpu_profile(**overrides):
    return ResourceProfile(**{**dict(
        profile_id="test-only", revision=1, task_type="finalize_segment",
        required_capabilities=[], workload_bounds={"input_bytes": 100},
        gpu_count=0, gpu_backend="none", gpu_vram_mib=0, host_ram_mib=100,
        execution_ram_mib=100, cpu_millicores=100, scratch_mib=0,
        scratch_pool="test-volume", execution_timeout_seconds=10,
        staging_timeout_seconds=60, evidence="test fixture", validation_state="unvalidated",
    ), **overrides})


@pytest.mark.parametrize("changes", [
    {"host_ram_mib": -1}, {"host_ram_mib": True}, {"host_ram_mib": float("nan")},
    {"gpu_vram_mib": 1}, {"gpu_count": 1}, {"execution_ram_mib": 101},
    {"gpu_count": False},
    {"staging_timeout_seconds": 901}, {"evidence": ""}, {"surprise": True},
])
def test_reject_invalid_budgets(changes):
    with pytest.raises(ValidationError):
        cpu_profile(**changes)


def test_explicit_zero_gpu_and_unknown_are_different():
    assert cpu_profile().gpu_vram_mib == 0
    with pytest.raises(ValidationError):
        cpu_profile(gpu_vram_mib=None)
    with pytest.raises(ValidationError):
        Capacity(allocatable=1, available=2)


@pytest.mark.parametrize("backend", ["cuda", "vulkan", "dx12"])
def test_gpu_backend_requires_a_real_resource_budget(backend):
    assert cpu_profile(gpu_count=1, gpu_backend=backend, gpu_vram_mib=100).gpu_backend == backend
    with pytest.raises(ValidationError):
        cpu_profile(gpu_backend=backend)
    with pytest.raises(ValidationError):
        cpu_profile(gpu_count=1, gpu_backend=backend)


@pytest.mark.parametrize("parents", [{"a": "missing"}, {"a": "a"}, {"a": "b", "b": "a"}])
def test_execution_hierarchy_rejects_missing_parents_and_cycles(parents):
    with pytest.raises(ValidationError, match="execution scope"):
        HostSnapshot(host_id=uuid4(), boot_id=uuid4(), sequence=1, captured_at=datetime.now(timezone.utc),
            host_ram={"allocatable": 100, "available": 100}, cpu_millicores=1000, scratch_pools={}, gpus={},
            execution_scopes={name: {"parent": parent, "ram": {"allocatable": 100, "available": 100},
                                    "cpu_millicores": 1000} for name, parent in parents.items()})


def test_obsolete_protocol_rejected():
    with pytest.raises(ValidationError):
        ClaimRequest(protocol_version=1, worker_instance_id="invalid", claim_request_id="invalid", task_types=[])


def test_set_serialization_is_stable_for_durable_replay():
    assert cpu_profile(required_capabilities=["z", "a"]).model_dump(mode="json")["required_capabilities"] == ["a", "z"]
    request = ClaimRequest(protocol_version=2, worker_instance_id=uuid4(), claim_request_id=uuid4(), task_types=["z", "a"])
    assert request.model_dump(mode="json")["task_types"] == ["a", "z"]


def test_claim_journal_survives_lost_response_and_process_restart(tmp_path):
    path = tmp_path / "private-worker-journal.sqlite"
    instance = uuid4()
    types = frozenset(["spatial_recon"])
    req = ClaimJournal(path).prepare(instance, types)
    # No response was recorded: transport/poll retries and a new client reuse it.
    journal = ClaimJournal(path)
    assert journal.prepare(instance, types) == req
    assert journal.pending() == (req, None)
    with pytest.raises(AdmissionError, match="previous_claim_unresolved"):
        journal.prepare(uuid4(), types)
    with pytest.raises(AdmissionError, match="previous_claim_unresolved"):
        journal.acknowledge_no_work(req.claim_request_id)
    journal.record_response(req.claim_request_id, None)
    journal.acknowledge_no_work(req.claim_request_id)
    assert journal.prepare(instance, types).claim_request_id != req.claim_request_id


@pytest.mark.asyncio
async def test_v2_retry_after_is_not_shortened_by_jitter_or_legacy_cap(tmp_path, monkeypatch):
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    monkeypatch.setattr("task_worker_api.client.random.uniform", lambda low, high: high)
    calls = []

    async def handle(request):
        calls.append(request)
        return httpx.Response(503 if len(calls) == 1 else 204, headers={"Retry-After": "86400"})

    snapshot = HostSnapshot(host_id=uuid4(), boot_id=uuid4(), sequence=1,
                            captured_at=datetime.now(timezone.utc), host_ram={"allocatable": 1, "available": 1},
                            cpu_millicores=1, execution_scopes={}, scratch_pools={}, gpus={})
    report = SignedHostReport(authority_id=uuid4(), epoch=1, report=snapshot, signature="0" * 64)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="http://test") as client:
        backend = BackendClient("http://test", "test", client=client, max_retries=2, retry_backoff_max_s=60)
        result, delay = await backend.resource_claim(ClaimJournal(tmp_path / "journal.sqlite"), uuid4(), ["test"], report)
    assert result is None and delay == 86400
    assert sleeps == [86401]
    assert len(calls) == 2 and calls[0].content == calls[1].content
