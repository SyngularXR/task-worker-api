"""Bounded ledger of terminal reports the backend never confirmed.

``Worker`` reports every task exactly once — ``complete()`` on success,
``fail()`` otherwise — and both calls already retry transient failures inside
:class:`~task_worker_api.client.BackendClient`. When that retry window is
exhausted (a backend down longer than the ~60s terminal budget, an nginx 502
storm, a restart that outlasts it) the outcome used to be *dropped*: logged at
ERROR and forgotten. The task then sits ``in_progress`` until the backend's
stale-task sweeper reclaims it and hands it to a worker that recomputes it
from scratch — GPU-hours redone for a result that already exists, and a
``failed`` task that never shows up as failed anywhere.

This ledger keeps those reports so the worker can re-send them:

* :meth:`UnconfirmedReports.pending` drives the poll loop's flush — one more
  attempt per poll cycle (busy or idle; a saturated queue never goes idle,
  and its reports must not sit here until they are evicted), and only while
  the backend is answering claims.
* :meth:`UnconfirmedReports.take` is the re-claim path, and it *discards*:
  a fresh delivery of a task we hold a report for is a new attempt, so the
  handler runs and the stale report is dropped. See ``Worker._run_one``.

Every entry carries the ``Idempotency-Key`` its first attempt used, so a
re-send is the *same* logical report rather than a second one: a backend that
dedupes on the key applies it once, and SynPusher's guarded terminal
transitions already ignore a duplicate write today.

The ledger is in-memory and bounded — a worker shouting into a dead backend
for hours must not grow without limit. Eviction is logged at ERROR, because an
evicted entry is an outcome nobody will ever record.
"""
from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

# How many unconfirmed reports one worker keeps. A worker runs one task at a
# time, so >1 pending entry already means several consecutive reports were
# lost; 64 covers a long outage while capping the memo at a few KB.
DEFAULT_MAX_UNCONFIRMED = 64


@dataclass
class TerminalReport:
    """One terminal report, plus the identity that makes re-sending it safe.

    ``payload`` is the handler result dict for ``kind="complete"`` and the
    error string for ``kind="fail"`` — i.e. exactly the second argument of
    :meth:`BackendClient.complete <task_worker_api.client.BackendClient.complete>`
    / :meth:`BackendClient.fail <task_worker_api.client.BackendClient.fail>`.

    ``idempotency_key`` is the name every delivery of this one outcome
    travels under, so a backend that dedupes on it applies the report once no
    matter how many times the wire ate the response.
    """

    task_id: int
    kind: str
    payload: object
    idempotency_key: str


class UnconfirmedReports:
    """LRU-bounded ``task_id → TerminalReport`` map. Not thread-safe.

    One instance per :class:`~task_worker_api.worker.Worker`; every method is
    plain in-memory bookkeeping, so nothing here raises or blocks the loop.
    """

    def __init__(self, max_entries: int = DEFAULT_MAX_UNCONFIRMED) -> None:
        self._entries: "OrderedDict[int, TerminalReport]" = OrderedDict()
        self._max_entries = max(1, int(max_entries))

    def __len__(self) -> int:
        return len(self._entries)

    def record(self, report: TerminalReport) -> None:
        """Remember an outcome the backend has not confirmed.

        A second report for the same task replaces the first — a task has one
        terminal outcome, and the newer attempt is the one worth re-sending.
        """
        self._entries[report.task_id] = report
        self._entries.move_to_end(report.task_id)
        while len(self._entries) > self._max_entries:
            task_id, dropped = self._entries.popitem(last=False)
            log.error(
                "task %s: dropping the unconfirmed %s report (ledger is full "
                "at %d entries); its outcome will never reach the backend and "
                "the task will be swept as stale",
                task_id, dropped.kind, self._max_entries,
            )

    def discard(self, task_id: int) -> None:
        """Forget a task's report — it landed, or something else owns it now."""
        self._entries.pop(task_id, None)

    def take(self, task_id: int) -> Optional[TerminalReport]:
        """Pop a task's report, or None. Used when the task is re-delivered."""
        return self._entries.pop(task_id, None)

    def pending(self) -> list:
        """Snapshot of every held report, oldest first.

        A snapshot (not a view) so the caller can discard entries as it
        delivers them without mutating what it is iterating over.
        """
        return list(self._entries.values())
