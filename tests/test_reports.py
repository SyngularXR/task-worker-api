"""Idempotent terminal reporting: the ledger, the replay, and the flush.

A terminal report that never lands used to be dropped, and that dropped
outcome is what makes a worker do the same job twice: the row stays
``in_progress``, the backend's stale-task sweeper re-queues it, and the next
claim recomputes hours of GPU work whose result already exists (and whose
outputs were already published — uploads happen *before* the report).

These tests pin the three parts of the fix:

* the reports ledger keeps an unconfirmed outcome, bounded and LRU;
* the poll loop re-sends it once the backend answers claims again, under the
  *same* ``Idempotency-Key`` as the attempt that failed, so a backend that
  dedupes on the key sees one terminal write, not two;
* a re-claim of a task we already completed replays the stored result instead
  of running the handler again — while a re-claim after an unconfirmed
  *failure* is a fresh attempt and must run normally.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from task_worker_api import TaskType
from task_worker_api.client import BackendClient
from task_worker_api.reports import TerminalReport, UnconfirmedReports
from task_worker_api.testing import FakeBackendClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _report(task_id: int, kind: str = "complete") -> TerminalReport:
    return TerminalReport(task_id, kind, {"planes": []}, f"task-{task_id}-{kind}-k")


class _FailingTerminalClient(FakeBackendClient):
    """Terminal reports fail the first ``fail_times`` calls, then land.

    Models the real shape of the bug: the handler's work is done and its
    outputs are already published, and only the terminal report is lost.
    """

    def __init__(self, *, fail_times: int = 1, exc: Exception | None = None) -> None:
        super().__init__()
        self._remaining = fail_times
        self._exc = exc or RuntimeError("backend unreachable")
        self.complete_calls: list = []
        self.fail_calls: list = []

    async def complete(self, task_id, result, *, idempotency_key=None) -> None:
        self.complete_calls.append((task_id, idempotency_key))
        if self._remaining > 0:
            self._remaining -= 1
            raise self._exc
        await super().complete(task_id, result, idempotency_key=idempotency_key)

    async def fail(self, task_id, error, *, idempotency_key=None) -> None:
        self.fail_calls.append((task_id, idempotency_key))
        if self._remaining > 0:
            self._remaining -= 1
            raise self._exc
        await super().fail(task_id, error, idempotency_key=idempotency_key)


def _queue_stl_task(client, tmp_path, *, task_id: int | None = None):
    stl = tmp_path / "fake.stl"
    stl.write_bytes(b"solid\nendsolid\n")
    if task_id is not None:
        client._next_id = task_id
    return client.queue_task(
        task_type=TaskType.DETECT_CUT_PLANES,
        params={"input_path": str(stl)},
    )


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------

def test_ledger_records_takes_and_discards():
    ledger = UnconfirmedReports()
    ledger.record(_report(1))
    assert len(ledger) == 1
    assert ledger.take(1).task_id == 1
    # take() removes it — a replayed outcome must not stay queued.
    assert len(ledger) == 0
    assert ledger.take(1) is None

    ledger.record(_report(2))
    ledger.discard(2)
    assert len(ledger) == 0


def test_ledger_replaces_a_second_report_for_the_same_task():
    """A task has one terminal outcome; the newer report supersedes."""
    ledger = UnconfirmedReports()
    ledger.record(TerminalReport(7, "complete", {"a": 1}, "k1"))
    ledger.record(TerminalReport(7, "fail", "boom", "k2"))
    assert len(ledger) == 1
    entry = ledger.take(7)
    assert (entry.kind, entry.payload) == ("fail", "boom")


def test_ledger_evicts_oldest_and_says_so(caplog):
    """The ledger is bounded — a worker shouting into a dead backend for
    hours must not grow without limit — and an evicted entry is an outcome
    nobody will ever record, so it is logged at ERROR."""
    ledger = UnconfirmedReports(max_entries=2)
    with caplog.at_level("ERROR"):
        for task_id in (1, 2, 3):
            ledger.record(_report(task_id))

    assert ledger.task_ids() == [2, 3]
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    assert "task 1" in errors[0]


def test_ledger_sendable_skips_refused_entries():
    ledger = UnconfirmedReports()
    refused = _report(1)
    refused.sendable = False
    ledger.record(refused)
    ledger.record(_report(2))
    assert [r.task_id for r in ledger.sendable()] == [2]
    # ...but the refused entry is still there for a re-claim replay.
    assert ledger.take(1) is not None


# ---------------------------------------------------------------------------
# The wire: Idempotency-Key
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_terminal_reports_send_the_idempotency_key_header():
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers.get("idempotency-key")))
        return httpx.Response(200, json={})

    client = BackendClient(
        "http://fake/api/v1", "k", worker_id="w",
        client=httpx.AsyncClient(
            base_url="http://fake/api/v1",
            transport=httpx.MockTransport(handler),
        ),
    )
    await client.complete(1, {"ok": True}, idempotency_key="task-1-complete-abc")
    await client.fail(2, "boom", idempotency_key="task-2-fail-def")
    # Omitting the key must not put an empty header on the wire — an older
    # deployment of this SDK sends none at all, and the backend treats both
    # the same way.
    await client.complete(3, {"ok": True})
    await client.close()

    assert seen == [
        ("/api/v1/tasks/1/complete", "task-1-complete-abc"),
        ("/api/v1/tasks/2/fail", "task-2-fail-def"),
        ("/api/v1/tasks/3/complete", None),
    ]


@pytest.mark.asyncio
async def test_retries_of_one_report_reuse_one_key():
    """The client's own retry loop must not mint a new identity per attempt —
    that is exactly the duplicate a deduping backend needs to collapse."""
    keys: list = []
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        keys.append(request.headers.get("idempotency-key"))
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={})

    client = BackendClient(
        "http://fake/api/v1", "k", worker_id="w",
        retry_backoff_s=0.0, retry_jitter=False,
        client=httpx.AsyncClient(
            base_url="http://fake/api/v1",
            transport=httpx.MockTransport(handler),
        ),
    )
    await client.complete(1, {"ok": True}, idempotency_key="task-1-complete-abc")
    await client.close()

    assert len(keys) == 3
    assert set(keys) == {"task-1-complete-abc"}


@pytest.mark.asyncio
async def test_client_override_without_the_key_still_reports(
    make_worker, tmp_path, caplog,
):
    """A consumer who subclassed the client (or wrote a double) against the
    old ``complete(task_id, result)`` signature must not have their worker
    broken by this upgrade — least of all on the one call whose job is to make
    sure a finished task's outcome is recorded. They lose the dedupe name, get
    one WARNING, and keep reporting."""
    import task_worker_api.worker as worker_module

    class _LegacyClient(FakeBackendClient):
        async def complete(self, task_id, result):
            await FakeBackendClient.complete(self, task_id, result)

    client = _LegacyClient()
    _queue_stl_task(client, tmp_path)

    async def handler(ctx, params):
        return {"planes": []}

    worker = make_worker(
        client=client, handlers={TaskType.DETECT_CUT_PLANES: handler},
    )
    worker_module._warned_legacy_terminal.clear()
    with caplog.at_level("WARNING"):
        await worker.run_one()

    assert len(client.completed_tasks) == 1
    assert client.completed_tasks[0] == {"task_id": 1, "result": {"planes": []}}
    assert client.keys_for(1) == [None]
    warnings = [
        r.getMessage() for r in caplog.records
        if "no 'idempotency_key' parameter" in r.getMessage()
    ]
    assert len(warnings) == 1


# ---------------------------------------------------------------------------
# The worker: keeping, re-sending, and replaying an unconfirmed report
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_lost_complete_is_kept_and_re_sent_with_the_same_key(
    make_worker, tmp_path,
):
    """The whole point: a complete() that dies past the client's retries is
    not dropped. The next idle poll cycle re-sends it — same report, same
    key — and the task finally lands terminal without recomputing anything."""
    client = _FailingTerminalClient(fail_times=1)
    _queue_stl_task(client, tmp_path)

    async def handler(ctx, params):
        return {"planes": [{"rank": 0}]}

    worker = make_worker(
        client=client, handlers={TaskType.DETECT_CUT_PLANES: handler},
    )
    await worker.run_one()

    assert client.completed_tasks == []      # the report was lost...
    assert len(worker._unconfirmed) == 1     # ...but not forgotten.

    await worker._flush_unconfirmed_reports()

    assert len(client.completed_tasks) == 1
    assert client.completed_tasks[0]["result"] == {"planes": [{"rank": 0}]}
    # Same identity on both sends: one logical terminal write, twice
    # delivered, which is precisely what the key lets the backend collapse.
    first_key, second_key = client.complete_calls[0][1], client.complete_calls[1][1]
    assert first_key is not None
    assert first_key == second_key
    # Confirmed → the ledger lets it go.
    assert len(worker._unconfirmed) == 0


@pytest.mark.asyncio
async def test_lost_fail_is_re_sent_too(make_worker, tmp_path):
    """A lost failure report is just as bad as a lost completion: nothing
    anywhere records that the task failed until the sweeper guesses."""
    client = _FailingTerminalClient(fail_times=1)
    _queue_stl_task(client, tmp_path)

    async def handler(ctx, params):
        raise RuntimeError("handler boom")

    worker = make_worker(
        client=client, handlers={TaskType.DETECT_CUT_PLANES: handler},
    )
    await worker.run_one()
    assert client.failed_tasks == []

    await worker._flush_unconfirmed_reports()

    assert len(client.failed_tasks) == 1
    assert "handler boom" in client.failed_tasks[0]["error"]
    assert client.fail_calls[0][1] == client.fail_calls[1][1]


@pytest.mark.asyncio
async def test_flush_is_skipped_while_claims_are_failing(make_worker, tmp_path):
    """Re-sending into a backend that isn't answering costs a full terminal
    retry budget per entry and undoes the claim backoff. The poll loop only
    flushes on a cycle whose claim reached the backend.

    That health signal is the *last* claim's result, so it is always one cycle
    stale — an outage starting mid-cycle can still cost a single transitional
    re-send, and no gate can prevent that. What must not happen is a re-send
    on every cycle for as long as the backend stays dark, so this pins the
    sustained case: already inside the outage, the flush stops entirely.
    """
    client = _FailingTerminalClient(fail_times=1)
    _queue_stl_task(client, tmp_path)

    async def handler(ctx, params):
        return {"planes": []}

    worker = make_worker(
        client=client, handlers={TaskType.DETECT_CUT_PLANES: handler},
        poll_interval_s=0.01,
    )
    await worker.run_one()
    assert len(worker._unconfirmed) == 1

    # Backend goes dark: every claim raises, so no cycle is "healthy".
    async def dead_claim(task_types, worker_id):
        raise RuntimeError("backend unreachable")

    client.claim_next = dead_claim
    # The state every cycle after the first one of an outage is in.
    worker._claim_failures = 1
    sends_after_task = len(client.complete_calls)

    async def stop_soon():
        while worker._claim_failures < 5:
            await asyncio.sleep(0)
        await worker.shutdown()

    await asyncio.gather(worker.run_forever(), stop_soon())

    assert len(client.complete_calls) == sends_after_task
    assert len(worker._unconfirmed) == 1


@pytest.mark.asyncio
async def test_unconfirmed_report_is_re_sent_while_the_queue_never_goes_idle(
    make_worker, tmp_path,
):
    """The flush must not be gated on the poll loop's idle branch.

    A worker facing a continuously nonempty queue never gets an idle cycle, so
    an idle-only flush would hold a lost outcome until the bounded ledger
    evicted it — and an evicted outcome is the dropped report this whole
    mechanism exists to prevent: the row stays ``in_progress``, the sweeper
    re-queues it, and its GPU-hours are spent again for artifacts that were
    already published. Here every claim hands back another task and the
    re-send still has to happen.
    """
    client = _FailingTerminalClient(fail_times=1)

    async def handler(ctx, params):
        return {"planes": []}

    worker = make_worker(
        client=client, handlers={TaskType.DETECT_CUT_PLANES: handler},
        poll_interval_s=0.01,
    )

    # A queue that is never empty: top it up before every claim, so the poll
    # loop never once takes the idle branch.
    busy_claim = client.claim_next

    async def always_busy_claim(task_types, worker_id):
        _queue_stl_task(client, tmp_path)
        return await busy_claim(task_types, worker_id)

    client.claim_next = always_busy_claim

    async def stop_after_a_few_tasks():
        while len(client.completed_tasks) < 3:
            await asyncio.sleep(0)
        await worker.shutdown()

    await asyncio.gather(worker.run_forever(), stop_after_a_few_tasks())

    # Task 1's complete() was lost; it was re-sent mid-stream, under the same
    # Idempotency-Key, without the queue ever draining.
    task_1_sends = [k for tid, k in client.complete_calls if tid == 1]
    assert len(task_1_sends) == 2
    assert len(set(task_1_sends)) == 1
    assert 1 in [r["task_id"] for r in client.completed_tasks]
    assert len(worker._unconfirmed) == 0


@pytest.mark.asyncio
async def test_re_claimed_completed_task_is_replayed_not_recomputed(
    make_worker, tmp_path, caplog,
):
    """The sweeper re-queues the task whose completion we could not report.
    Re-running the handler would redo work whose outputs are already
    published — replay the stored result instead."""
    client = _FailingTerminalClient(fail_times=1)
    task = _queue_stl_task(client, tmp_path)

    runs = []

    async def handler(ctx, params):
        runs.append(ctx.task.id)
        return {"planes": [{"rank": 0}]}

    worker = make_worker(
        client=client, handlers={TaskType.DETECT_CUT_PLANES: handler},
    )
    await worker.run_one()
    assert runs == [task.id]
    assert len(worker._unconfirmed) == 1

    # Same row, re-queued and handed back to this worker.
    _queue_stl_task(client, tmp_path, task_id=task.id)
    with caplog.at_level("WARNING"):
        await worker.run_one()

    assert runs == [task.id], "the handler must not run a second time"
    assert len(client.completed_tasks) == 1
    assert client.completed_tasks[0]["result"] == {"planes": [{"rank": 0}]}
    assert client.complete_calls[0][1] == client.complete_calls[1][1]
    assert len(worker._unconfirmed) == 0
    assert any(
        "already completed" in r.getMessage() for r in caplog.records
    )


@pytest.mark.asyncio
async def test_replay_heartbeats_while_it_re_sends(make_worker, tmp_path):
    """The replay goes through the client's retry schedule like any terminal
    report, so it needs the same heartbeat cover: a task whose ``updated_at``
    freezes for that window is what the sweeper reclaims — which is how the
    task came back here to begin with."""

    class _SlowReplayClient(_FailingTerminalClient):
        def __init__(self) -> None:
            super().__init__(fail_times=1)
            self.progress_events_during_replay: int | None = None

        async def complete(self, task_id, result, *, idempotency_key=None):
            if self._remaining == 0:      # the replay, not the first attempt
                await asyncio.sleep(0.05)
                self.progress_events_during_replay = len(self.progress_events)
            await super().complete(task_id, result, idempotency_key=idempotency_key)

    client = _SlowReplayClient()
    task = _queue_stl_task(client, tmp_path)

    async def handler(ctx, params):
        return {"planes": []}

    worker = make_worker(
        client=client, handlers={TaskType.DETECT_CUT_PLANES: handler},
        heartbeat_interval_s=0.01,
    )
    await worker.run_one()
    before_replay = len(client.progress_events)

    _queue_stl_task(client, tmp_path, task_id=task.id)
    await worker.run_one()

    assert len(client.completed_tasks) == 1
    assert client.progress_events_during_replay > before_replay


@pytest.mark.asyncio
async def test_re_claimed_task_after_a_lost_fail_runs_again(
    make_worker, tmp_path,
):
    """A re-delivery after an unconfirmed *failure* is a genuine retry: the
    backend re-queued the task to be attempted again. Running the handler is
    the whole point, and the stale fail report must not survive to stamp
    ``failed`` over whatever this attempt produces."""
    client = _FailingTerminalClient(fail_times=1)
    task = _queue_stl_task(client, tmp_path)

    runs = []

    async def handler(ctx, params):
        runs.append(ctx.task.id)
        if len(runs) == 1:
            raise RuntimeError("handler boom")
        return {"planes": []}

    worker = make_worker(
        client=client, handlers={TaskType.DETECT_CUT_PLANES: handler},
    )
    await worker.run_one()
    assert client.failed_tasks == []
    assert len(worker._unconfirmed) == 1

    _queue_stl_task(client, tmp_path, task_id=task.id)
    await worker.run_one()

    assert runs == [task.id, task.id], "the retry must actually run"
    assert len(client.completed_tasks) == 1
    # The superseded failure never reached the backend.
    assert client.failed_tasks == []
    assert len(worker._unconfirmed) == 0


@pytest.mark.asyncio
async def test_backend_refusal_stops_re_sends_but_keeps_the_outcome(
    make_worker, tmp_path,
):
    """A 4xx is the backend answering "not yours to report" (the ownership
    check, after the sweeper reassigned the task). Re-sending on a timer
    would just repeat the rejection every cycle — but the outcome is kept, so
    a later re-claim can still replay it instead of recomputing."""
    client = _FailingTerminalClient(fail_times=1)
    _queue_stl_task(client, tmp_path)

    async def handler(ctx, params):
        return {"planes": []}

    worker = make_worker(
        client=client, handlers={TaskType.DETECT_CUT_PLANES: handler},
    )
    await worker.run_one()

    request = httpx.Request("PUT", "http://fake/api/v1/tasks/1/complete")
    refusal = httpx.HTTPStatusError(
        "403", request=request, response=httpx.Response(403, request=request),
    )

    async def refusing_complete(task_id, result, *, idempotency_key=None):
        client.complete_calls.append((task_id, idempotency_key))
        raise refusal

    client.complete = refusing_complete

    await worker._flush_unconfirmed_reports()
    sends = len(client.complete_calls)
    await worker._flush_unconfirmed_reports()

    assert len(client.complete_calls) == sends, "refused reports stop retrying"
    assert len(worker._unconfirmed) == 1, "but the outcome is still held"


@pytest.mark.asyncio
async def test_flush_stops_at_the_first_degraded_send(make_worker, tmp_path):
    """A transport failure means the backend went away again mid-flush.
    Walking the rest of the ledger into the same wall would stall the poll
    loop for a full terminal retry budget per entry."""
    client = FakeBackendClient()
    worker = make_worker(client=client)
    for task_id in (1, 2, 3):
        worker._unconfirmed.record(_report(task_id))

    calls = []

    async def dead_complete(task_id, result, *, idempotency_key=None):
        calls.append(task_id)
        raise RuntimeError("backend unreachable")

    client.complete = dead_complete
    await worker._flush_unconfirmed_reports()

    assert calls == [1]
    assert len(worker._unconfirmed) == 3


@pytest.mark.asyncio
async def test_idle_poll_cycle_flushes_when_the_backend_answers(
    make_worker, tmp_path,
):
    """End-to-end through run_forever: the report lost on one cycle lands on
    the next idle cycle, with no operator action and no recomputation."""
    client = _FailingTerminalClient(fail_times=1)
    _queue_stl_task(client, tmp_path)

    async def handler(ctx, params):
        return {"planes": []}

    worker = make_worker(
        client=client, handlers={TaskType.DETECT_CUT_PLANES: handler},
        poll_interval_s=0.01,
    )

    async def stop_after_flush():
        while not client.completed_tasks:
            await asyncio.sleep(0)
        await worker.shutdown()

    await asyncio.wait_for(
        asyncio.gather(worker.run_forever(), stop_after_flush()), timeout=5,
    )

    assert len(client.completed_tasks) == 1
    assert len(worker._unconfirmed) == 0


@pytest.mark.asyncio
async def test_shutdown_shouts_about_outcomes_it_could_not_deliver(
    make_worker, caplog,
):
    """If the process exits with reports still unconfirmed, they die with the
    ledger — an operator has to hear about it."""
    client = FakeBackendClient()
    worker = make_worker(client=client, poll_interval_s=0.01)
    worker._unconfirmed.record(_report(41))
    worker._unconfirmed.record(_report(42))

    await worker.shutdown()
    with caplog.at_level("ERROR"):
        await worker.run_forever()

    errors = [
        r.getMessage() for r in caplog.records
        if r.levelname == "ERROR" and "never confirmed" in r.getMessage()
    ]
    assert len(errors) == 1
    assert "41, 42" in errors[0]
