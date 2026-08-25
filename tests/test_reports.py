"""Idempotent terminal reporting: the ledger and the flush.

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
* a re-delivery of a task we hold a report for is a *new attempt*: the
  handler runs and the stale report is dropped, because nothing in the claim
  envelope proves the delivery belongs to the attempt that filed the report.
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
    # take() removes it — a superseded outcome must not stay queued.
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

    assert [r.task_id for r in ledger.pending()] == [2, 3]
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    assert "task 1" in errors[0]


def test_ledger_evicts_on_payload_bytes_not_just_entry_count(caplog):
    """Entry count bounds nothing on its own: ``payload`` is whatever the
    handler returned, so a handful of fat completions is unbounded memory in
    a process that runs for weeks."""
    ledger = UnconfirmedReports(max_entries=64, max_bytes=2048)
    with caplog.at_level("ERROR"):
        for task_id in (1, 2, 3):
            ledger.record(
                TerminalReport(task_id, "complete", {"blob": "x" * 900}, "k"),
            )

    # Nowhere near the 64-entry bound, and still evicted.
    assert [r.task_id for r in ledger.pending()] == [2, 3]
    assert ledger.nbytes <= 2048
    assert any("task 1" in r.getMessage() for r in caplog.records)


def test_ledger_drops_a_report_larger_than_its_whole_budget(caplog):
    """A single payload over budget evicts itself rather than living on as an
    unbounded exception — the outcome was headed for the sweeper anyway."""
    ledger = UnconfirmedReports(max_bytes=512)
    with caplog.at_level("ERROR"):
        ledger.record(TerminalReport(9, "complete", {"blob": "x" * 5000}, "k"))

    assert len(ledger) == 0
    assert ledger.nbytes == 0
    assert any("task 9" in r.getMessage() for r in caplog.records)


def test_ledger_releases_bytes_when_a_report_leaves():
    """Accounting has to survive every exit path, or the budget leaks shut."""
    ledger = UnconfirmedReports(max_bytes=4096)
    ledger.record(TerminalReport(1, "complete", {"blob": "x" * 1000}, "k"))
    ledger.record(TerminalReport(2, "fail", "boom", "k"))
    assert ledger.nbytes > 1000

    ledger.take(1)
    ledger.discard(2)
    assert ledger.nbytes == 0

    # A superseding report replaces the first one's bytes, not adds to them.
    ledger.record(TerminalReport(3, "complete", {"blob": "x" * 1000}, "k"))
    held = ledger.nbytes
    ledger.record(TerminalReport(3, "complete", {"blob": "x" * 1000}, "k2"))
    assert ledger.nbytes == held


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
# The worker: keeping and re-sending an unconfirmed report
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
async def test_flush_waits_for_an_empty_queue_and_then_re_sends(
    make_worker, tmp_path,
):
    """The flush is gated on a claim that came back *empty*.

    While the queue keeps handing back work, the worker has no evidence that
    the task it holds a report for isn't sitting in that queue waiting to be
    re-run — a claim that returns some *other* task says nothing about ours.
    So the report waits. The moment the queue drains, the same claim proves
    nothing is pending and the report goes out, under its original key.
    """
    client = _FailingTerminalClient(fail_times=1)

    async def handler(ctx, params):
        return {"planes": []}

    worker = make_worker(
        client=client, handlers={TaskType.DETECT_CUT_PLANES: handler},
        poll_interval_s=0.01,
    )

    # A queue that is never empty: top it up before every claim, so the poll
    # loop never once takes the idle branch — until ``busy`` is cleared.
    busy = True
    base_claim = client.claim_next

    async def maybe_busy_claim(task_types, worker_id):
        if busy:
            _queue_stl_task(client, tmp_path)
        return await base_claim(task_types, worker_id)

    client.claim_next = maybe_busy_claim

    sends_while_busy = []

    async def drain_then_stop():
        nonlocal busy
        # Three tasks after the lost one, all served without an idle cycle.
        while len(client.completed_tasks) < 3:
            await asyncio.sleep(0)
        sends_while_busy.extend(k for tid, k in client.complete_calls if tid == 1)
        busy = False
        while not any(r["task_id"] == 1 for r in client.completed_tasks):
            await asyncio.sleep(0)
        await worker.shutdown()

    await asyncio.wait_for(
        asyncio.gather(worker.run_forever(), drain_then_stop()), timeout=5,
    )

    # Busy cycles: only the attempt's own lost send. No re-send raced a
    # re-queue we could not have seen.
    assert len(sends_while_busy) == 1
    # Drained: the held outcome lands, same report, same identity.
    task_1_sends = [k for tid, k in client.complete_calls if tid == 1]
    assert len(task_1_sends) == 2
    assert len(set(task_1_sends)) == 1
    assert [r["result"] for r in client.completed_tasks if r["task_id"] == 1] == [
        {"planes": []}
    ]
    assert len(worker._unconfirmed) == 0


@pytest.mark.asyncio
async def test_a_re_queued_task_is_never_terminalized_from_the_ledger(
    make_worker, tmp_path,
):
    """Regression: the flush must not beat the re-delivery to the row.

    The sweeper has re-queued a task this worker still holds a report for, so
    the row is ``pending`` and waiting for a genuine re-run. If the poll loop
    re-sends the held report first, the backend takes it, the row goes
    terminal on the *earlier* attempt's output, and the re-run the backend
    asked for is silently skipped — the exact outcome ``_run_one``'s
    new-attempt rule exists to prevent. The report has to wait for a claim
    that says nothing is pending, which by construction cannot happen while
    this task is queued.
    """
    client = _FailingTerminalClient(fail_times=1)
    task = _queue_stl_task(client, tmp_path)

    attempts = []

    async def handler(ctx, params):
        attempts.append(ctx.task.id)
        return {"attempt": len(attempts)}

    worker = make_worker(
        client=client, handlers={TaskType.DETECT_CUT_PLANES: handler},
        poll_interval_s=0.01,
    )
    await worker.run_one()
    assert client.completed_tasks == []      # attempt 1's report was lost
    stale_key = worker._unconfirmed.pending()[0].idempotency_key

    # The sweeper reclaims the row and queues it for a re-run.
    _queue_stl_task(client, tmp_path, task_id=task.id)

    async def stop_after_the_rerun():
        while len(attempts) < 2 or not client.completed_tasks:
            await asyncio.sleep(0)
        await worker.shutdown()

    await asyncio.wait_for(
        asyncio.gather(worker.run_forever(), stop_after_the_rerun()), timeout=5,
    )

    assert attempts == [task.id, task.id], "the re-run actually ran"
    # One terminal write, and it is the re-run's — the stale result never
    # reached the backend, in either order.
    assert [r["result"] for r in client.completed_tasks] == [{"attempt": 2}]
    assert stale_key not in client.keys_for(task.id)
    assert len(worker._unconfirmed) == 0


@pytest.mark.asyncio
async def test_re_delivered_completed_task_runs_again_and_drops_the_report(
    make_worker, tmp_path, caplog,
):
    """A re-delivery is a new attempt, never an answer from the ledger.

    The held report says nothing about *which* attempt this delivery is: the
    claim envelope carries no backend-issued attempt or lease id, so a task
    coming back after a completion whose *response* was merely lost looks
    exactly like the backend legitimately re-queuing it for a genuine re-run
    (an operator requeue, a retry of work that has to happen again). Answering
    from the ledger would report a stale result and silently skip the work
    that was asked for, so the handler runs and the stale report is dropped.
    """
    client = _FailingTerminalClient(fail_times=1)
    task = _queue_stl_task(client, tmp_path)

    runs = []

    async def handler(ctx, params):
        runs.append(ctx.task.id)
        return {"planes": [{"rank": len(runs) - 1}]}

    worker = make_worker(
        client=client, handlers={TaskType.DETECT_CUT_PLANES: handler},
    )
    await worker.run_one()
    assert runs == [task.id]
    assert len(worker._unconfirmed) == 1     # complete() was lost

    # The same row, re-queued and handed back to this worker.
    _queue_stl_task(client, tmp_path, task_id=task.id)
    with caplog.at_level("WARNING"):
        await worker.run_one()

    assert runs == [task.id, task.id], "the re-delivery must run the handler"
    # ...and what the backend records is *this* attempt's result, not the
    # stale one the ledger was holding.
    assert len(client.completed_tasks) == 1
    assert client.completed_tasks[0]["result"] == {"planes": [{"rank": 1}]}
    # A new attempt reports under a new identity, so a deduping backend does
    # not collapse it into the earlier report.
    assert client.complete_calls[0][1] != client.complete_calls[1][1]
    assert len(worker._unconfirmed) == 0, "the superseded report is dropped"
    assert any("supersedes it" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_re_claimed_task_after_a_lost_fail_runs_again(
    make_worker, tmp_path,
):
    """The same rule with a failure in the ledger, where the cost of getting
    it wrong is loudest: a stale fail report that survived would stamp
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
async def test_backend_refusal_stops_re_sends_and_drops_the_outcome(
    make_worker, tmp_path,
):
    """A 4xx is the backend answering "not yours to report" (the ownership
    check, after the sweeper reassigned the task). Re-sending on a timer would
    just repeat the rejection every cycle while holding a slot in the bounded
    ledger, and whoever owns the task now reports its outcome."""
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
    assert len(worker._unconfirmed) == 0, "and are not held for a re-delivery"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [408, 429])
async def test_transient_4xx_keeps_the_report_in_the_ledger(
    make_worker, tmp_path, status,
):
    """408 and 429 are 4xx but *transient*: the client already retried them
    and merely ran out of budget. Retiring the entry on one of those would
    leave a rate-limited outcome held but never sent again — the dropped
    report and duplicate GPU work this ledger exists to prevent."""
    client = FakeBackendClient()
    worker = make_worker(client=client)
    worker._unconfirmed.record(_report(1))

    request = httpx.Request("PUT", "http://fake/api/v1/tasks/1/complete")
    calls = []

    async def throttled_complete(task_id, result, *, idempotency_key=None):
        calls.append(task_id)
        raise httpx.HTTPStatusError(
            str(status), request=request,
            response=httpx.Response(status, request=request),
        )

    client.complete = throttled_complete
    await worker._flush_unconfirmed_reports()
    await worker._flush_unconfirmed_reports()

    assert calls == [1, 1], "a throttled report keeps being re-sent"
    assert worker._unconfirmed.pending(), "and stays in the ledger"

    # ...and once the rate-limit window passes, it lands.
    client.complete = FakeBackendClient.complete.__get__(client)
    await worker._flush_unconfirmed_reports()
    assert len(client.completed_tasks) == 1
    assert len(worker._unconfirmed) == 0


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
