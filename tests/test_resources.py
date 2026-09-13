"""Trust-boundary resource validation, without inventing production budgets."""
import pytest
import asyncio
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import httpx
from pydantic import ValidationError

from task_worker_api.claim_journal import ClaimJournal
from task_worker_api.client import BackendClient, _MAX_FAIL_ERROR_BYTES, _TERMINAL_MIN_ATTEMPTS
from task_worker_api.resources import AdmissionError
from task_worker_api.resources import (AttemptOwnership, Capacity, ClaimRequest, ClaimResult,
                                        ResourceProfile, HostSnapshot)
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


# ---------------------------------------------------------------------------
# v2 fail() — error-string cap at the admission wire boundary
# ---------------------------------------------------------------------------


def _admitted_claim(instance):
    now = datetime.now(timezone.utc)
    return ClaimResult(
        task_id=1,
        task={"id": 1, "task_type": "finalize_segment", "case_id": None,
              "item_key": "k", "params": {}, "inputs": {}},
        ownership=AttemptOwnership(worker_instance_id=instance, attempt_id=uuid4(),
                                   generation=1, token="t" * 32),
        profile=cpu_profile(), input_digest="a" * 64, gpu_uuid=None,
        host_id=uuid4(), boot_id=uuid4(), execution_scope="test-scope",
        staging_deadline=now + timedelta(seconds=60),
        lease_expires_at=now + timedelta(seconds=60), state="running",
    )


def _admitted_journal(tmp_path, instance):
    journal = ClaimJournal(tmp_path / "claim.sqlite")
    request = journal.prepare(instance, frozenset({"finalize_segment"}))
    claim = _admitted_claim(instance)
    journal.record_response(request.claim_request_id, claim)
    return journal, claim


def _admitted_backend(claim, sent):
    def handle(request):
        sent.append(request.content)
        now = datetime.now(timezone.utc)
        return httpx.Response(200, json={
            "task_id": claim.task_id, "attempt_id": str(claim.ownership.attempt_id),
            "state": "released", "task_status": 3, "cancelled": False,
            "server_time": now.isoformat(),
            "lease_expires_at": (now + timedelta(seconds=60)).isoformat(),
            "execution_deadline": None, "progress": {},
        })

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="http://test")
    return BackendClient("http://test", "test", client=client), client


@pytest.mark.asyncio
async def test_v2_fail_caps_a_giant_error_before_it_is_journaled(tmp_path):
    """An uncapped traceback is worse on v2 than on v1: the durable request is
    persisted before transmission and replayed forever, so a body nginx rejects
    with 413 leaves the attempt unresolved and wedges the worker behind
    previous_claim_unresolved instead of just losing one report."""
    instance = uuid4()
    journal, claim = _admitted_journal(tmp_path, instance)
    sent: list = []
    backend, transport = _admitted_backend(claim, sent)
    error = ("RuntimeError: colmap failed\n" + "x" * 4_000_000
             + "\nCalledProcessError: returned non-zero exit status 1")
    await backend.resource_operation(journal, "fail", {"error": error, "failure_kind": "error"})
    await transport.aclose()

    assert len(sent) == 1 and len(sent[0]) <= _MAX_FAIL_ERROR_BYTES + 4096
    body = json.loads(sent[0])
    assert body["error"].startswith("RuntimeError: colmap failed")
    assert body["error"].endswith("CalledProcessError: returned non-zero exit status 1")
    assert "bytes truncated]..." in body["error"]
    assert body["failure_kind"] == "error"


@pytest.mark.asyncio
async def test_v2_fail_sanitizes_an_unencodable_error(tmp_path):
    """A subprocess message decoded with ``surrogateescape`` carries lone
    surrogates; httpx encodes strict UTF-8, so an unsanitized one raises while
    *building* every replay of the stored request — the attempt never resolves."""
    instance = uuid4()
    journal, claim = _admitted_journal(tmp_path, instance)
    sent: list = []
    backend, transport = _admitted_backend(claim, sent)
    await backend.resource_operation(
        journal, "fail", {"error": "boom \udcff", "failure_kind": "error"})
    await transport.aclose()

    assert json.loads(sent[0])["error"] == "boom \\udcff"


@pytest.mark.asyncio
async def test_v2_fail_replays_the_capped_body_after_a_lost_response(tmp_path):
    """Recovery replays the durable request verbatim, so the capped body — not
    the original — is what a restarted worker retransmits."""
    instance = uuid4()
    journal, claim = _admitted_journal(tmp_path, instance)
    sent: list = []
    backend, transport = _admitted_backend(claim, sent)
    error = "RuntimeError: boom\n" + "\udcff" * 200_000

    async def lost(*args, **kwargs):
        raise httpx.ConnectError("response lost")

    backend._resource_request = lost
    with pytest.raises(httpx.ConnectError):
        await backend.resource_operation(journal, "fail", {"error": error, "failure_kind": "error"})
    unresolved = journal.unresolved_operations()
    assert [kind for kind, _ in unresolved] == ["fail"]

    # Restart: a fresh client replays the stored request and it must be deliverable.
    replay, replay_transport = _admitted_backend(claim, sent)
    states = await replay.resource_recover_operations(journal)
    await transport.aclose()
    await replay_transport.aclose()

    assert [state.state for state in states] == ["released"]
    assert len(sent) == 1 and len(sent[0]) <= _MAX_FAIL_ERROR_BYTES + 4096
    assert "bytes truncated]..." in json.loads(sent[0])["error"]
    assert journal.unresolved_operations() == []


# ---------------------------------------------------------------------------
# v2 terminal reports — retry hardness parity with v1 complete/fail
# ---------------------------------------------------------------------------


def _flaky_backend(claim, statuses, calls, **options):
    """Backend whose lifecycle route returns ``statuses`` in order, then 200.

    ``retry_backoff_s=0`` keeps the retry loop's sleeps instant; the delay
    schedule itself is covered in ``test_client_retry``.
    """
    def handle(request):
        calls.append(request)
        status = statuses[len(calls) - 1] if len(calls) <= len(statuses) else 200
        if status != 200:
            return httpx.Response(status)
        now = datetime.now(timezone.utc)
        return httpx.Response(200, json={
            "task_id": claim.task_id, "attempt_id": str(claim.ownership.attempt_id),
            "state": "released", "task_status": 2, "cancelled": False,
            "server_time": now.isoformat(),
            "lease_expires_at": (now + timedelta(seconds=60)).isoformat(),
            "execution_deadline": None, "progress": {},
        })

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="http://test")
    return BackendClient("http://test", "test", client=client, retry_backoff_s=0, **options), client


def _terminal_payload(kind):
    return {"result": {}} if kind == "complete" else {"error": "boom", "failure_kind": "error"}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["complete", "fail"])
async def test_v2_terminal_report_retries_a_500_instead_of_orphaning_the_attempt(tmp_path, kind):
    """A 500 mid-write (the backend's own dependency dying) must not leave the
    durable operation unresolved — that wedges the worker behind
    previous_claim_unresolved until a supervisor restart replays it."""
    journal, claim = _admitted_journal(tmp_path, uuid4())
    calls: list = []
    backend, transport = _flaky_backend(claim, [500], calls)
    state = await backend.resource_operation(journal, kind, _terminal_payload(kind))
    await transport.aclose()

    assert len(calls) == 2 and state.attempt_id == claim.ownership.attempt_id
    assert journal.unresolved_operations() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["complete", "fail"])
async def test_v2_terminal_report_holds_the_six_attempt_floor(tmp_path, kind):
    """The floor rides out a backend restart that outlasts the default budget,
    even when the worker is configured with fewer attempts."""
    journal, claim = _admitted_journal(tmp_path, uuid4())
    calls: list = []
    backend, transport = _flaky_backend(claim, [502] * _TERMINAL_MIN_ATTEMPTS, calls, max_retries=2)
    with pytest.raises(httpx.HTTPStatusError):
        await backend.resource_operation(journal, kind, _terminal_payload(kind))
    await transport.aclose()

    assert len(calls) == _TERMINAL_MIN_ATTEMPTS
    assert [k for k, _ in journal.unresolved_operations()] == [kind]


@pytest.mark.asyncio
async def test_v2_non_terminal_operation_keeps_the_default_retry_contract(tmp_path):
    """Only the idempotent journal-replayed terminal kinds widen the set: a 500
    on ``start`` still surfaces immediately, without consuming retry budget."""
    journal, claim = _admitted_journal(tmp_path, uuid4())
    calls: list = []
    backend, transport = _flaky_backend(claim, [500], calls, max_retries=2)
    with pytest.raises(httpx.HTTPStatusError):
        await backend.resource_operation(journal, "start", {})
    await transport.aclose()

    assert len(calls) == 1


# ---------------------------------------------------------------------------
# v2 progress() — one-shot on the handler's critical path
# ---------------------------------------------------------------------------


def _running_state(claim):
    now = datetime.now(timezone.utc)
    return {
        "task_id": claim.task_id, "attempt_id": str(claim.ownership.attempt_id),
        "state": "running", "task_status": 2, "cancelled": False,
        "server_time": now.isoformat(),
        "lease_expires_at": (now + timedelta(seconds=60)).isoformat(),
        "execution_deadline": None, "progress": {},
    }


@pytest.fixture
def no_blocking_sleep(monkeypatch):
    """Records every backoff sleep the call under test starts, and never waits."""
    slept: list = []

    async def sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    return slept


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [410, 426])
async def test_v2_progress_maps_a_retired_protocol_to_protocol_error(status, no_blocking_sleep):
    """A bare HTTPStatusError here is swallowed by ``AttemptLease.update``'s
    generic handler, so the worker keeps executing an attempt the backend has
    stopped honouring; ProtocolError is what expires the lease."""
    from task_worker_api.errors import ProtocolError

    claim = _admitted_claim(uuid4())
    sent: list = []

    def handle(request):
        sent.append(request.content)
        return httpx.Response(status)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="http://test") as client:
        backend = BackendClient("http://test", "test", client=client)
        with pytest.raises(ProtocolError):
            await backend.resource_progress(claim, {"stage": "compute"})

    assert len(sent) == 1 and no_blocking_sleep == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["transport", "status"])
async def test_v2_progress_stays_one_shot_under_a_degraded_backend(failure, no_blocking_sleep):
    """Progress runs on the handler's critical path. Retrying it let a degraded
    backend block the handler for max_retries x lifecycle_timeout_s plus backoff
    (~74s on defaults) per update, while the work it describes sat idle. One
    attempt, no backoff sleep, error straight back to ``AttemptLease.update``."""
    claim = _admitted_claim(uuid4())
    sent: list = []

    def handle(request):
        sent.append(request.content)
        if failure == "transport":
            raise httpx.ConnectError("blip")
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="http://test") as client:
        backend = BackendClient("http://test", "test", client=client)
        with pytest.raises((httpx.TransportError, httpx.HTTPStatusError)):
            await backend.resource_progress(claim, {"stage": "compute"})

    assert len(sent) == 1, "progress must not spend the retry budget on the critical path"
    assert no_blocking_sleep == [], "progress must not sleep on backoff"


@pytest.mark.asyncio
async def test_v2_progress_ignores_an_uncapped_retry_after(no_blocking_sleep):
    """The v2 lifecycle path passes ``retry_after_max_s=None``, so a 429 on the
    retried path can impose an arbitrary server-named sleep. One-shot progress
    never honours it: a throttling backend cannot park the handler for an hour."""
    claim = _admitted_claim(uuid4())
    sent: list = []

    def handle(request):
        sent.append(request.content)
        return httpx.Response(429, headers={"Retry-After": "3600"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="http://test") as client:
        backend = BackendClient("http://test", "test", client=client)
        with pytest.raises(httpx.HTTPStatusError) as exc:
            await backend.resource_progress(claim, {"stage": "compute"})

    assert exc.value.response.status_code == 429
    assert len(sent) == 1 and no_blocking_sleep == []


@pytest.mark.asyncio
async def test_v2_progress_returns_the_attempt_state_it_was_given():
    claim = _admitted_claim(uuid4())
    sent: list = []

    def handle(request):
        sent.append(request.content)
        return httpx.Response(200, json=_running_state(claim))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="http://test") as client:
        backend = BackendClient("http://test", "test", client=client)
        state = await backend.resource_progress(claim, {"stage": "compute", "current": 1, "total": 2})

    assert len(sent) == 1 and state.attempt_id == claim.ownership.attempt_id
    assert json.loads(sent[0])["progress"] == {"stage": "compute", "current": 1, "total": 2}


@pytest.mark.asyncio
async def test_v2_lifecycle_calls_other_than_progress_still_retry(no_blocking_sleep):
    """The one-shot carve-out is progress only — heartbeat renews the lease from
    a background task, where riding out a blip is worth the wait."""
    claim = _admitted_claim(uuid4())
    sent: list = []

    def handle(request):
        sent.append(request.content)
        if len(sent) == 1:
            raise httpx.ConnectError("blip")
        return httpx.Response(200, json=_running_state(claim))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="http://test") as client:
        backend = BackendClient("http://test", "test", client=client)
        state = await backend.resource_heartbeat(claim)

    assert len(sent) == 2 and state.attempt_id == claim.ownership.attempt_id
    assert no_blocking_sleep, "heartbeat keeps its backoff"


@pytest.mark.asyncio
@pytest.mark.parametrize("bounded", [False, True])
async def test_v2_heartbeat_retries_are_bounded_by_the_remaining_lease(bounded, no_blocking_sleep):
    """Renewal is the one v2 caller whose retries have a hard deadline. The v2
    path passes ``retry_after_max_s=None``, so one 429 can name an hour-long
    sleep: unbounded, the renewal returns long after the lease it was renewing
    expired — by which time ``AttemptLease`` has cancelled the owner task
    mid-work and hard-exited the worker. Given the lease's remaining time, the
    call gives up instead of sleeping past it, and ``_renew`` tries again on
    its own cadence."""
    claim = _admitted_claim(uuid4())
    sent: list = []

    def handle(request):
        sent.append(request.content)
        return httpx.Response(429, headers={"Retry-After": "3600"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="http://test") as client:
        backend = BackendClient("http://test", "test", client=client, lifecycle_timeout_s=15.0)
        with pytest.raises(httpx.HTTPStatusError):
            await backend.resource_heartbeat(claim, **({"remaining_lease_s": 30.0} if bounded else {}))

    if bounded:
        assert len(sent) == 1 and no_blocking_sleep == []
    else:
        assert sum(no_blocking_sleep) > 3600, "the unbounded path is what parks the renewal"


@pytest.mark.asyncio
async def test_v2_heartbeat_still_rides_out_a_blip_inside_a_long_lease(no_blocking_sleep):
    """The budget bounds retries, it doesn't disable them: a lease with room to
    spare still spends attempts and backoff on a transient failure."""
    claim = _admitted_claim(uuid4())
    sent: list = []

    def handle(request):
        sent.append(request.content)
        if len(sent) == 1:
            raise httpx.ConnectError("blip")
        return httpx.Response(200, json=_running_state(claim))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="http://test") as client:
        backend = BackendClient("http://test", "test", client=client, lifecycle_timeout_s=15.0)
        state = await backend.resource_heartbeat(claim, remaining_lease_s=300.0)

    assert len(sent) == 2 and state.attempt_id == claim.ownership.attempt_id
    assert no_blocking_sleep and sum(no_blocking_sleep) <= 240, no_blocking_sleep


@pytest.mark.asyncio
@pytest.mark.parametrize("lifecycle_timeout_s", [3.0, 40.0])
async def test_v2_progress_uses_the_configured_lifecycle_timeout(lifecycle_timeout_s):
    """Progress hardcoded a 5s deadline, so a consumer that widened (or
    tightened) ``lifecycle_timeout_s`` for its backend got neither."""
    claim = _admitted_claim(uuid4())
    deadlines: list = []

    def handle(request):
        deadlines.append(request.extensions["timeout"]["read"])
        return httpx.Response(200, json=_running_state(claim))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="http://test") as client:
        backend = BackendClient("http://test", "test", client=client,
                                lifecycle_timeout_s=lifecycle_timeout_s)
        await backend.resource_progress(claim, {"stage": "compute"})

    assert deadlines == [lifecycle_timeout_s]
