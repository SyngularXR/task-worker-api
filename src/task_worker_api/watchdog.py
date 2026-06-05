"""Wall-clock per-task watchdog — terminates runaway tasks off the event loop.

Runs in an OS thread so the deadline fires even when a handler blocks the
asyncio loop. On the deadline it SIGTERM→SIGKILLs the task-spawned child
processes (snapshot delta, matched by pid + create_time); if the task still
hasn't finished (an in-process wedge with nothing to kill) it does a bounded
synchronous fail() then a hard exit, and the container's restart policy
recovers.

Design rules (see the design spec):
  - The watchdog NEVER touches the loop-bound async BackendClient. It only
    kills processes and, in the last-resort path, calls an injected sync fail.
  - All OS interactions (enumerate children, kill, clock, sleep, exit, sync
    fail) are injectable so the escalation ladder is unit-testable with no
    real processes and no real time.
"""
from __future__ import annotations

import logging
import os
import signal
import threading
import time
from typing import Callable, Optional

log = logging.getLogger(__name__)

# SIGKILL is absent on Windows; fall back to SIGTERM so import + tests work
# cross-platform. Production workers are Linux containers where both exist.
_SIGTERM = signal.SIGTERM
_SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)

# (pid, create_time) — create_time disambiguates a reused PID.
ProcId = tuple[int, str]


class TerminalGuard:
    """Exactly-once gate shared by the loop reporter and the watchdog thread.

    Whoever calls ``claim()`` first wins the sole right to send the terminal
    status. Thread-safe across the event-loop thread and the watchdog thread.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._claimed = False

    def claim(self) -> bool:
        with self._lock:
            if self._claimed:
                return False
            self._claimed = True
            return True


def _read_stat(pid: int) -> Optional[tuple[int, str]]:
    """Return (ppid, starttime) for a pid from /proc/<pid>/stat, or None.

    The comm field (field 2) can contain spaces and parens, so we split on the
    last ')' and index the rest. starttime is overall field 22 → index 19 of
    the post-comm slice (0-based). starttime is a per-pid-stable identity.
    """
    try:
        with open(f"/proc/{pid}/stat", "r") as fh:
            data = fh.read()
    except (OSError, ValueError):
        return None
    rparen = data.rfind(")")
    if rparen == -1:
        return None
    fields = data[rparen + 2:].split()
    try:
        return int(fields[1]), fields[19]
    except (IndexError, ValueError):
        return None


def list_descendants(pid: int) -> set[ProcId]:
    """All descendant ``(pid, create_time)`` of ``pid`` via /proc (Linux).

    Best-effort: PIDs that vanish or deny access mid-walk are skipped. Returns
    an empty set when /proc is unavailable (non-Linux) — the watchdog then has
    nothing to kill and correctly escalates to the hard-exit path.
    """
    if not os.path.isdir("/proc"):
        return set()
    try:
        all_pids = [int(n) for n in os.listdir("/proc") if n.isdigit()]
    except OSError:
        return set()

    info: dict[int, tuple[int, str]] = {}
    for p in all_pids:
        st = _read_stat(p)
        if st is not None:
            info[p] = st

    children_by_parent: dict[int, list[int]] = {}
    for p, (ppid, _) in info.items():
        children_by_parent.setdefault(ppid, []).append(p)

    out: set[ProcId] = set()
    stack = list(children_by_parent.get(pid, []))
    while stack:
        c = stack.pop()
        st = info.get(c)
        if st is None:
            continue
        out.add((c, st[1]))
        stack.extend(children_by_parent.get(c, []))
    return out


def kill_procs(procs: set[ProcId], sig: int) -> None:
    """Send ``sig`` to each ``(pid, create_time)``, skipping PIDs whose current
    create_time no longer matches (PID reused) or that have already exited."""
    for pid, starttime in procs:
        st = _read_stat(pid)
        if st is None or st[1] != starttime:
            continue  # gone, or PID reused by an unrelated process
        try:
            os.kill(pid, sig)
        except OSError:
            pass


class TaskWatchdog:
    """One per task. Fires once at the deadline, then escalates."""

    def __init__(
        self,
        *,
        timeout_s: float,
        grace_s: float,
        guard: TerminalGuard,
        sync_fail: Callable[[str], None],
        on_hard_exit: Callable[[], None],
        children_before: set,
        list_descendants_fn: Callable[[int], set] = list_descendants,
        kill_fn: Callable[[set, int], None] = kill_procs,
        now_fn: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], None] = time.sleep,
        tick_s: float = 0.5,
        worker_pid: Optional[int] = None,
    ):
        self.timeout_s = timeout_s
        self.grace_s = grace_s
        self.guard = guard
        self._sync_fail = sync_fail
        self._on_hard_exit = on_hard_exit
        self._children_before = children_before
        self._list = list_descendants_fn
        self._kill = kill_fn
        self._now = now_fn
        self._sleep = sleep_fn
        self._tick = tick_s
        self._pid = worker_pid if worker_pid is not None else os.getpid()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.fired = False

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="task-watchdog", daemon=True,
        )
        self._thread.start()

    def stop(self) -> bool:
        """Signal the task finished. Non-blocking (never joins on the caller's
        thread, so it is safe to call from the event loop). Returns whether the
        deadline had already fired."""
        self._stop.set()
        return self.fired

    # ----- internals ------------------------------------------------

    def _wait(self, seconds: float) -> bool:
        """Sleep up to ``seconds`` in ticks; return True if stopped early."""
        end = self._now() + seconds
        while self._now() < end:
            if self._stop.is_set():
                return True
            self._sleep(min(self._tick, max(0.0, end - self._now())))
        return self._stop.is_set()

    def _task_spawned(self) -> set:
        return self._list(self._pid) - self._children_before

    def _run(self) -> None:
        if self._wait(self.timeout_s):
            return  # finished before the deadline
        self.fired = True
        log.warning(
            "task watchdog: deadline %.0fs exceeded; terminating task work",
            self.timeout_s,
        )
        # Phase 1 — SIGTERM the task-spawned children.
        self._kill(self._task_spawned(), _SIGTERM)
        if self._wait(self.grace_s):
            return  # killing unblocked the handler; loop resumed and will report
        # Phase 2 — SIGKILL survivors (re-enumerate to catch late spawns).
        self._kill(self._task_spawned(), _SIGKILL)
        if self._wait(self.grace_s):
            return
        # Phase 3 — in-process wedge: nothing killable freed the loop.
        if self.guard.claim():
            try:
                self._sync_fail(f"timeout: exceeded {self.timeout_s:.0f}s (hard-exit)")
            except Exception as e:  # noqa: BLE001
                log.warning("watchdog sync_fail failed: %s", e)
        self._on_hard_exit()
