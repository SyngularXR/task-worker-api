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
  attempt per poll cycle whose claim reached the backend and came back
  *empty*. An empty queue is the worker's only evidence that the task has not
  been re-queued for a fresh attempt; re-sending without it can terminalize a
  task the backend asked to have re-run. A worker whose queue never drains
  therefore holds its reports, possibly until they are evicted here.
* :meth:`UnconfirmedReports.take` is the re-claim path, and it *discards*:
  a fresh delivery of a task we hold a report for is a new attempt, so the
  handler runs and the stale report is dropped. See ``Worker._run_one``.

Every entry carries the ``Idempotency-Key`` its first attempt used, so a
re-send is the *same* logical report rather than a second one: a backend that
dedupes on the key applies it once, and SynPusher's guarded terminal
transitions already ignore a duplicate write today.

The ledger is in-memory and bounded by entry *count* and by total payload
*bytes* — a worker shouting into a dead backend for hours must not grow
without limit, and a completion payload is an arbitrary handler result dict
(an inline manifest, a per-frame array), so counting entries alone bounds
nothing. Eviction is logged at ERROR, because an evicted entry is an outcome
nobody will ever record.
"""
from __future__ import annotations

import json
import logging
from collections import OrderedDict
from dataclasses import dataclass
from functools import cached_property
from typing import Optional

log = logging.getLogger(__name__)

# How many unconfirmed reports one worker keeps. A worker runs one task at a
# time, so >1 pending entry already means several consecutive reports were
# lost; 64 covers a long outage.
DEFAULT_MAX_UNCONFIRMED = 64

# ...and how many bytes of payload they may hold between them. The count above
# says nothing about size: ``payload`` is whatever the handler returned, so 64
# entries is 64 × "as big as a result dict gets". 4 MiB keeps the ledger a
# rounding error next to the task data a worker already holds, and still fits
# far more than the handful of reports a real outage strands.
DEFAULT_MAX_UNCONFIRMED_BYTES = 4 * 1024 * 1024


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

    @cached_property
    def size_bytes(self) -> int:
        """What holding this report costs, measured as its encoded payload.

        The wire form is the honest unit and the cheap one: ``payload`` is an
        arbitrary handler result, so walking it for a true footprint would be
        both slower and less relevant than the JSON the re-send will carry.
        Cached — the ledger re-checks its budget on every record, and a large
        payload must not be re-encoded each time. Nothing here raises: an
        unencodable payload (a cycle) still has to be *bounded*, and its repr
        is a serviceable proxy.
        """
        try:
            return len(json.dumps(self.payload, default=str).encode())
        except Exception:  # noqa: BLE001
            return len(repr(self.payload).encode())


class UnconfirmedReports:
    """LRU-bounded ``task_id → TerminalReport`` map. Not thread-safe.

    Bounded twice over: by entry count, and by the total encoded size of the
    payloads held (see :data:`DEFAULT_MAX_UNCONFIRMED_BYTES`). Whichever bound
    trips first evicts oldest-first.

    One instance per :class:`~task_worker_api.worker.Worker`; every method is
    plain in-memory bookkeeping, so nothing here raises or blocks the loop.
    """

    def __init__(
        self,
        max_entries: int = DEFAULT_MAX_UNCONFIRMED,
        max_bytes: int = DEFAULT_MAX_UNCONFIRMED_BYTES,
    ) -> None:
        self._entries: "OrderedDict[int, TerminalReport]" = OrderedDict()
        self._max_entries = max(1, int(max_entries))
        self._max_bytes = max(1, int(max_bytes))
        self._bytes = 0

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def nbytes(self) -> int:
        """Encoded payload bytes currently held."""
        return self._bytes

    def record(self, report: TerminalReport) -> None:
        """Remember an outcome the backend has not confirmed.

        A second report for the same task replaces the first — a task has one
        terminal outcome, and the newer attempt is the one worth re-sending.
        """
        superseded = self._entries.pop(report.task_id, None)
        if superseded is not None:
            self._bytes -= superseded.size_bytes
        self._entries[report.task_id] = report
        self._bytes += report.size_bytes
        # Oldest-first, until *both* bounds hold. A single report bigger than
        # the whole byte budget evicts itself on the last pass rather than
        # being kept as an unbounded exception — the ledger is a convenience,
        # and the outcome it drops is one the backend was going to sweep
        # anyway; unbounded growth in a long-running worker is not.
        while self._entries and (
            len(self._entries) > self._max_entries
            or self._bytes > self._max_bytes
        ):
            task_id, dropped = self._entries.popitem(last=False)
            self._bytes -= dropped.size_bytes
            log.error(
                "task %s: dropping the unconfirmed %s report (ledger is full "
                "at %d entries / %d payload bytes); its outcome will never "
                "reach the backend and the task will be swept as stale",
                task_id, dropped.kind, self._max_entries, self._max_bytes,
            )

    def discard(self, task_id: int) -> None:
        """Forget a task's report — it landed, or something else owns it now."""
        self.take(task_id)

    def take(self, task_id: int) -> Optional[TerminalReport]:
        """Pop a task's report, or None. Used when the task is re-delivered."""
        report = self._entries.pop(task_id, None)
        if report is not None:
            self._bytes -= report.size_bytes
        return report

    def pending(self) -> list:
        """Snapshot of every held report, oldest first.

        A snapshot (not a view) so the caller can discard entries as it
        delivers them without mutating what it is iterating over.
        """
        return list(self._entries.values())
