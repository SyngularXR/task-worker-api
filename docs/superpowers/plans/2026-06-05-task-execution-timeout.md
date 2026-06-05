# Task Execution Timeout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the `task_worker_api` SDK a per-task execution timeout (default 30 min, per-type overrides) that auto-terminates a runaway task — even one that blocks the asyncio event loop — and fails it terminally.

**Architecture:** A per-task OS **watchdog thread** holds a `monotonic()` deadline off the event loop. On expiry it SIGTERM→SIGKILLs the task-spawned child processes (snapshot delta, matched by pid+`create_time`); if the task still hasn't finished (in-process wedge) it does a bounded synchronous `fail()` then a hard exit (container `restart: unless-stopped` recovers). The watchdog never touches the loop-bound async client. A `TerminalGuard` (lock+flag) makes terminal reporting exactly-once, first-resolver-wins.

**Tech Stack:** Python ≥3.10, asyncio, threading, `/proc` (Linux containers), `urllib` (stdlib, for the bounded sync fail), pytest + pytest-asyncio (auto mode). No new third-party deps.

**Spec:** [`docs/superpowers/specs/2026-06-05-task-execution-timeout-design.md`](../specs/2026-06-05-task-execution-timeout-design.md)

---

## File Structure

- **Create** `src/task_worker_api/timeouts.py` — pure config resolution (env + constructor → effective seconds). No I/O, trivially testable.
- **Create** `src/task_worker_api/watchdog.py` — `TerminalGuard`, `list_descendants`/`kill_procs` (/proc helpers), `TaskWatchdog` (the escalation ladder, all OS seams injectable).
- **Modify** `src/task_worker_api/worker.py` — new `Worker.__init__` params; `_make_sync_fail` helper; `_run_one` wires the watchdog + single guarded terminal report.
- **Create** `tests/test_timeouts.py` — config resolution + env parsing.
- **Create** `tests/test_watchdog.py` — the escalation ladder with faked clock/enumerate/kill/exit.
- **Create** `tests/test_worker_timeout.py` — `Worker` wiring (disabled / fires→report / fast-completes-no-fire) via `FakeBackendClient` + an injectable watchdog.
- **Modify** `pyproject.toml` + `CHANGELOG.md` — version `0.6.1 → 0.7.0`.

Run tests with: `pytest -q` (from repo root, after `pip install -e ".[dev]"`).

---

## Task 1: Timeout config resolution (`timeouts.py`)

**Files:**
- Create: `src/task_worker_api/timeouts.py`
- Test: `tests/test_timeouts.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_timeouts.py
from __future__ import annotations

from task_worker_api.enums import TaskType
from task_worker_api.timeouts import (
    DEFAULT_TASK_TIMEOUT_S,
    parse_timeouts_env,
    resolve_task_timeout,
)


def test_parse_env_basic():
    assert parse_timeouts_env("default=1800,gs_build=7200") == {
        "default": 1800.0, "gs_build": 7200.0,
    }


def test_parse_env_empty_and_none():
    assert parse_timeouts_env("") == {}
    assert parse_timeouts_env(None) == {}


def test_parse_env_skips_malformed(caplog):
    with caplog.at_level("WARNING"):
        out = parse_timeouts_env("default=1800, oops, render=abc , gs_build=60")
    assert out == {"default": 1800.0, "gs_build": 60.0}
    assert any("WORKER_TASK_TIMEOUTS" in r.message for r in caplog.records)


def test_resolution_precedence_env_per_type_wins():
    # env per-type > ctor per-type > env default > ctor default
    t = resolve_task_timeout(
        TaskType.GS_BUILD,
        default_s=1800.0,
        per_type={TaskType.GS_BUILD: 3600.0},
        env={"gs_build": 7200.0, "default": 600.0},
    )
    assert t == 7200.0


def test_resolution_ctor_per_type_then_env_default_then_ctor_default():
    assert resolve_task_timeout(
        TaskType.GS_BUILD, default_s=1800.0,
        per_type={TaskType.GS_BUILD: 3600.0}, env={"default": 600.0},
    ) == 3600.0
    assert resolve_task_timeout(
        TaskType.RENDER, default_s=1800.0, per_type={}, env={"default": 600.0},
    ) == 600.0
    assert resolve_task_timeout(
        TaskType.RENDER, default_s=1800.0, per_type={}, env={},
    ) == 1800.0


def test_zero_disables():
    assert resolve_task_timeout(
        TaskType.RENDER, default_s=1800.0, per_type={}, env={"render": 0.0},
    ) == 0.0


def test_default_constant():
    assert DEFAULT_TASK_TIMEOUT_S == 1800.0
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_timeouts.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'task_worker_api.timeouts'`

- [ ] **Step 3: Implement `timeouts.py`**

```python
# src/task_worker_api/timeouts.py
"""Resolve per-task execution timeouts from constructor config + environment.

Pure functions — no I/O — so resolution is trivially testable. Resolution
order for a task type, highest priority first:

  env per-type  →  constructor per-type  →  env default  →  constructor default

A resolved value <= 0 means "no timeout" (escape hatch for a known-unbounded
task type).
"""
from __future__ import annotations

import logging
from typing import Optional

from .enums import TaskType

log = logging.getLogger(__name__)

DEFAULT_TASK_TIMEOUT_S = 1800.0  # 30 minutes


def parse_timeouts_env(raw: Optional[str]) -> dict[str, float]:
    """Parse ``WORKER_TASK_TIMEOUTS='default=1800,gs_build=7200'`` into a dict.

    Keys are lowercased task-type values plus the literal ``default``. Malformed
    entries are skipped with a WARNING; this never raises (a bad env var must
    not crash worker startup).
    """
    result: dict[str, float] = {}
    if not raw:
        return result
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            log.warning(
                "WORKER_TASK_TIMEOUTS: ignoring malformed entry %r (no '=')", part
            )
            continue
        key, _, val = part.partition("=")
        key = key.strip().lower()
        try:
            result[key] = float(val.strip())
        except ValueError:
            log.warning(
                "WORKER_TASK_TIMEOUTS: ignoring non-numeric value in %r", part
            )
    return result


def resolve_task_timeout(
    task_type: TaskType,
    *,
    default_s: float,
    per_type: dict[TaskType, float],
    env: dict[str, float],
) -> float:
    """Resolve the effective timeout (seconds) for ``task_type``. <=0 = disabled."""
    tv = task_type.value
    if tv in env:
        return env[tv]
    if task_type in per_type:
        return per_type[task_type]
    if "default" in env:
        return env["default"]
    return default_s
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_timeouts.py -q`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add src/task_worker_api/timeouts.py tests/test_timeouts.py
git commit -m "feat(timeout): per-task timeout config resolution"
```

---

## Task 2: `TerminalGuard` (exactly-once reporter gate)

**Files:**
- Create: `src/task_worker_api/watchdog.py` (start the file here)
- Test: `tests/test_watchdog.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_watchdog.py
from __future__ import annotations

import threading

from task_worker_api.watchdog import TerminalGuard


def test_guard_first_claim_wins_once():
    g = TerminalGuard()
    assert g.claim() is True
    assert g.claim() is False
    assert g.claim() is False


def test_guard_is_thread_safe_single_winner():
    g = TerminalGuard()
    winners: list[bool] = []
    lock = threading.Lock()
    barrier = threading.Barrier(20)

    def worker():
        barrier.wait()
        won = g.claim()
        with lock:
            winners.append(won)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert winners.count(True) == 1
    assert winners.count(False) == 19
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_watchdog.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'task_worker_api.watchdog'`

- [ ] **Step 3: Implement the file header + `TerminalGuard`**

```python
# src/task_worker_api/watchdog.py
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
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_watchdog.py -q`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add src/task_worker_api/watchdog.py tests/test_watchdog.py
git commit -m "feat(timeout): TerminalGuard exactly-once reporter gate"
```

---

## Task 3: `/proc` descendant enumeration + kill helpers

**Files:**
- Modify: `src/task_worker_api/watchdog.py` (append helpers)
- Test: `tests/test_watchdog.py` (append; Linux-only integration test)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_watchdog.py  (append)
import subprocess
import sys
import time as _time

import pytest

from task_worker_api.watchdog import list_descendants, kill_procs, _SIGKILL

_LINUX = sys.platform.startswith("linux")


@pytest.mark.skipif(not _LINUX, reason="/proc enumeration is Linux-only")
def test_list_descendants_finds_child_then_kill_removes_it():
    # Spawn a child that sleeps; assert it shows up as a descendant, then kill.
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        descendants = list_descendants(os.getpid())
        pids = {pid for pid, _ in descendants}
        assert proc.pid in pids

        ours = {(pid, ct) for pid, ct in descendants if pid == proc.pid}
        kill_procs(ours, _SIGKILL)
        # Child should die promptly.
        proc.wait(timeout=5)
        assert proc.returncode is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


@pytest.mark.skipif(not _LINUX, reason="/proc enumeration is Linux-only")
def test_kill_procs_skips_reused_pid():
    # A (pid, wrong-create_time) must NOT be killed.
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        kill_procs({(proc.pid, "0")}, _SIGKILL)  # bogus create_time
        _time.sleep(0.3)
        assert proc.poll() is None  # still alive — we refused to kill it
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_list_descendants_empty_off_proc(monkeypatch):
    # When /proc is absent (e.g. dev on macOS/Windows), return empty set.
    monkeypatch.setattr("task_worker_api.watchdog.os.path.isdir", lambda p: False)
    assert list_descendants(1) == set()
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_watchdog.py -q`
Expected: FAIL — `ImportError: cannot import name 'list_descendants'`

- [ ] **Step 3: Implement the helpers (append to `watchdog.py`)**

```python
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
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_watchdog.py -q`
Expected: PASS (the `test_list_descendants_empty_off_proc` test runs everywhere; the two `_LINUX` tests run on Linux/CI, skip on the Windows dev box)

- [ ] **Step 5: Commit**

```bash
git add src/task_worker_api/watchdog.py tests/test_watchdog.py
git commit -m "feat(timeout): /proc descendant enumeration + pid-reuse-safe kill"
```

---

## Task 4: `TaskWatchdog` escalation ladder

**Files:**
- Modify: `src/task_worker_api/watchdog.py` (append `TaskWatchdog`)
- Test: `tests/test_watchdog.py` (append)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_watchdog.py  (append)
from task_worker_api.watchdog import TaskWatchdog, TerminalGuard


class _FakeClock:
    """Deterministic monotonic clock. `sleep` advances time instantly and
    lets a registered callback fire side effects at the right virtual time."""

    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _make_wd(clock, *, timeout_s, grace_s, list_fn, kill_calls, guard=None,
             hard_exit_calls=None, sync_fail_calls=None, children_before=None):
    return TaskWatchdog(
        timeout_s=timeout_s,
        grace_s=grace_s,
        guard=guard or TerminalGuard(),
        sync_fail=lambda err: (sync_fail_calls or []).append(err),
        on_hard_exit=lambda: (hard_exit_calls if hard_exit_calls is not None else []).append(True),
        children_before=children_before or set(),
        list_descendants_fn=list_fn,
        kill_fn=lambda procs, sig: kill_calls.append((set(procs), sig)),
        now_fn=clock.now,
        sleep_fn=clock.sleep,
        tick_s=1.0,
        worker_pid=1234,
    )


def test_no_fire_when_stopped_before_deadline():
    clock = _FakeClock()
    kill_calls = []
    wd = _make_wd(clock, timeout_s=100.0, grace_s=10.0,
                  list_fn=lambda pid: set(), kill_calls=kill_calls)
    wd.start()
    wd.stop()              # signal completion immediately
    wd._thread.join(timeout=5)
    assert wd.fired is False
    assert kill_calls == []


def test_fires_and_kills_task_spawned_children():
    # Deadline passes; loop "resumes" during the SIGTERM grace (stop set), so
    # no hard exit. Asserts SIGTERM targeted only the task-spawned delta.
    clock = _FakeClock()
    kill_calls, hard_exit = [], []
    before = {(10, "100")}
    after = {(10, "100"), (11, "200")}   # pid 11 spawned by the task
    wd = _make_wd(clock, timeout_s=5.0, grace_s=10.0,
                  list_fn=lambda pid: after, kill_calls=kill_calls,
                  hard_exit_calls=hard_exit, children_before=before)
    wd.start()
    # Let the watchdog cross the deadline + issue SIGTERM, then signal the
    # loop resumed (handler unblocked) so it stops before SIGKILL/hard-exit.
    import time as _t
    for _ in range(50):
        if kill_calls:
            break
        _t.sleep(0.02)
    wd.stop()
    wd._thread.join(timeout=5)
    assert wd.fired is True
    assert hard_exit == []
    assert kill_calls[0][0] == {(11, "200")}        # only the new child
    assert kill_calls[0][1] == _SIGTERM


def test_hard_exit_when_nothing_killable_and_loop_never_resumes():
    clock = _FakeClock()
    kill_calls, hard_exit, sync_fail = [], [], []
    guard = TerminalGuard()
    wd = _make_wd(clock, timeout_s=5.0, grace_s=10.0,
                  list_fn=lambda pid: set(),     # no descendants at all
                  kill_calls=kill_calls, guard=guard,
                  hard_exit_calls=hard_exit, sync_fail_calls=sync_fail)
    wd.start()
    wd._thread.join(timeout=5)   # never stopped → runs full ladder
    assert wd.fired is True
    assert len(hard_exit) == 1
    assert len(sync_fail) == 1
    assert "timeout" in sync_fail[0]
    assert guard.claim() is False   # watchdog already claimed the report


def test_hard_exit_skips_sync_fail_if_guard_already_claimed():
    clock = _FakeClock()
    hard_exit, sync_fail = [], []
    guard = TerminalGuard()
    assert guard.claim() is True   # loop already reported
    wd = _make_wd(clock, timeout_s=5.0, grace_s=10.0, list_fn=lambda pid: set(),
                  kill_calls=[], guard=guard,
                  hard_exit_calls=hard_exit, sync_fail_calls=sync_fail)
    wd.start()
    wd._thread.join(timeout=5)
    assert sync_fail == []          # guard was taken; no duplicate report
    assert len(hard_exit) == 1      # still hard-exits to recover the worker
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_watchdog.py -q`
Expected: FAIL — `ImportError: cannot import name 'TaskWatchdog'`

- [ ] **Step 3: Implement `TaskWatchdog` (append to `watchdog.py`)**

```python
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
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_watchdog.py -q`
Expected: PASS (all watchdog tests; Linux-only ones skip on Windows dev)

- [ ] **Step 5: Commit**

```bash
git add src/task_worker_api/watchdog.py tests/test_watchdog.py
git commit -m "feat(timeout): TaskWatchdog deadline + SIGTERM/SIGKILL/hard-exit ladder"
```

---

## Task 5: Bounded synchronous fail helper (`worker.py`)

**Files:**
- Modify: `src/task_worker_api/worker.py` (add `_make_sync_fail`)
- Test: `tests/test_worker_timeout.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_worker_timeout.py
from __future__ import annotations

import json

from task_worker_api.worker import _make_sync_fail


def test_make_sync_fail_builds_put_request(monkeypatch):
    captured = {}

    class _Resp:
        def close(self):
            captured["closed"] = True

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["timeout"] = timeout
        captured["auth"] = req.get_header("Authorization")
        captured["body"] = json.loads(req.data.decode())
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    fn = _make_sync_fail("http://bk/api/v1/", "secret", 42, timeout_s=3.0)
    fn("timeout: exceeded 1800s (hard-exit)")

    assert captured["url"] == "http://bk/api/v1/tasks/42/fail"
    assert captured["method"] == "PUT"
    assert captured["timeout"] == 3.0
    assert captured["auth"] == "Bearer secret"
    assert captured["body"] == {"error": "timeout: exceeded 1800s (hard-exit)"}
    assert captured["closed"] is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_worker_timeout.py -q`
Expected: FAIL — `ImportError: cannot import name '_make_sync_fail'`

- [ ] **Step 3: Implement `_make_sync_fail` in `worker.py`**

Add these imports near the top of `worker.py` (it already imports `asyncio`, `os`, `shutil`, `tempfile`, `traceback`):

```python
import json
import urllib.request
```

Add new imports from the package modules (next to the existing `from .cancel import CancelGuard`):

```python
from .timeouts import DEFAULT_TASK_TIMEOUT_S, parse_timeouts_env, resolve_task_timeout
from .watchdog import TaskWatchdog, TerminalGuard, list_descendants
```

Then add this module-level helper (above the `Worker` class):

```python
def _make_sync_fail(
    base_url: str, api_key: str, task_id: int, *, timeout_s: float = 3.0,
):
    """Build a synchronous ``fail(error)`` callable for the watchdog thread.

    The async BackendClient is bound to the (possibly blocked) event loop, so
    the watchdog's last-resort report uses plain stdlib urllib with an explicit
    short timeout — it must never become a second wedge. Matches the wire
    format of ``BackendClient.fail``: PUT /tasks/{id}/fail {"error": ...}.
    """
    url = f"{base_url.rstrip('/')}/tasks/{task_id}/fail"

    def _sync_fail(error: str) -> None:
        data = json.dumps({"error": error}).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="PUT",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        urllib.request.urlopen(req, timeout=timeout_s).close()

    return _sync_fail
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_worker_timeout.py -q`
Expected: PASS (1 test)

- [ ] **Step 5: Commit**

```bash
git add src/task_worker_api/worker.py tests/test_worker_timeout.py
git commit -m "feat(timeout): bounded synchronous fail helper for the watchdog"
```

---

## Task 6: Wire the timeout into `Worker.__init__` and `_run_one`

**Files:**
- Modify: `src/task_worker_api/worker.py:56-118` (`__init__`) and `src/task_worker_api/worker.py:262-340` (`_run_one`)
- Test: `tests/test_worker_timeout.py` (append)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_worker_timeout.py  (append)
import pytest

from task_worker_api import TaskType, Worker
from task_worker_api.testing import FakeBackendClient


class _ImmediateWatchdog:
    """Test double for TaskWatchdog: reports `fired` deterministically without
    threads/processes. Worker passes it via the _watchdog_factory seam."""
    last = None

    def __init__(self, *, fire: bool):
        self._fire = fire
        self.fired = False
        self.started = False
        self.stopped = False
        _ImmediateWatchdog.last = self

    def start(self):
        self.started = True

    def stop(self) -> bool:
        self.stopped = True
        self.fired = self._fire
        return self.fired


def _worker(fake, tmp_path, **kw):
    return Worker(
        backend_url="http://fake/api/v1", api_key="k", worker_id="w",
        handlers={TaskType.DETECT_CUT_PLANES: kw.pop("handler")},
        work_dir=str(tmp_path / "work"), client=fake, **kw,
    )


def _queue(fake, tmp_path):
    (tmp_path / "f.stl").write_bytes(b"solid\nendsolid\n")
    fake.queue_task(
        task_type=TaskType.DETECT_CUT_PLANES,
        params={"input_path": str(tmp_path / "f.stl")},
    )


@pytest.mark.asyncio
async def test_timeout_disabled_no_watchdog(tmp_path):
    fake = FakeBackendClient()
    _queue(fake, tmp_path)
    made = {"n": 0}

    def factory(**kwargs):
        made["n"] += 1
        return _ImmediateWatchdog(fire=False)

    async def handler(ctx, params):
        return {"ok": True}

    w = _worker(fake, tmp_path, handler=handler, task_timeout_s=0.0,
                _watchdog_factory=factory)
    await w.run_one()
    assert made["n"] == 0                      # disabled → no watchdog built
    assert len(fake.completed_tasks) == 1
    assert fake.failed_tasks == []


@pytest.mark.asyncio
async def test_timeout_fires_reports_timeout_fail(tmp_path):
    fake = FakeBackendClient()
    _queue(fake, tmp_path)

    async def handler(ctx, params):
        return {"ok": True}   # completes, but the watchdog reports it fired

    w = _worker(fake, tmp_path, handler=handler, task_timeout_s=60.0,
                _watchdog_factory=lambda **kw: _ImmediateWatchdog(fire=True))
    await w.run_one()
    assert fake.completed_tasks == []
    assert len(fake.failed_tasks) == 1
    assert "timeout: exceeded 60s" in fake.failed_tasks[0]["error"]
    assert _ImmediateWatchdog.last.started is True
    assert _ImmediateWatchdog.last.stopped is True


@pytest.mark.asyncio
async def test_no_timeout_normal_completion(tmp_path):
    fake = FakeBackendClient()
    _queue(fake, tmp_path)

    async def handler(ctx, params):
        return {"ok": True}

    w = _worker(fake, tmp_path, handler=handler, task_timeout_s=60.0,
                _watchdog_factory=lambda **kw: _ImmediateWatchdog(fire=False))
    await w.run_one()
    assert len(fake.completed_tasks) == 1
    assert fake.failed_tasks == []


@pytest.mark.asyncio
async def test_per_type_env_override_resolved(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKER_TASK_TIMEOUTS", "default=10,detect_cut_planes=0")
    fake = FakeBackendClient()
    _queue(fake, tmp_path)
    built = {"n": 0}

    async def handler(ctx, params):
        return {"ok": True}

    # detect_cut_planes resolves to 0 (disabled) → factory never called.
    w = _worker(fake, tmp_path, handler=handler,
                _watchdog_factory=lambda **kw: built.__setitem__("n", built["n"] + 1) or _ImmediateWatchdog(fire=False))
    await w.run_one()
    assert built["n"] == 0
    assert len(fake.completed_tasks) == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_worker_timeout.py -q`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'task_timeout_s'`

- [ ] **Step 3: Modify `Worker.__init__`**

Add these parameters to the signature (after `request_timeout_s: float = 30.0,`):

```python
        request_timeout_s: float = 30.0,
        task_timeout_s: float = DEFAULT_TASK_TIMEOUT_S,
        task_timeouts: Optional[dict] = None,
        timeout_grace_s: float = 15.0,
        on_hard_exit: Optional[Callable[[], None]] = None,
        client: Optional[BackendClient] = None,
        _watchdog_factory: Callable[..., object] = TaskWatchdog,
```

Add these assignments in the body (next to the other `self.* = *` lines, before the payload-logger build):

```python
        self.task_timeout_s = task_timeout_s
        self.task_timeouts = task_timeouts or {}
        self.timeout_grace_s = timeout_grace_s
        self._on_hard_exit = on_hard_exit or (lambda: os._exit(75))
        self._timeout_env = parse_timeouts_env(os.environ.get("WORKER_TASK_TIMEOUTS"))
        self._watchdog_factory = _watchdog_factory
```

- [ ] **Step 4: Replace `_run_one` body**

Replace the whole `_run_one` method (`worker.py:262-340`) with this version. It preserves every existing behavior (payload record, schema validate, prepare_inputs, heartbeat, `CancelGuard`, output upload, task-dir cleanup) and routes terminal reporting through one guard so timeout and normal completion can never double-report.

```python
    async def _run_one(self, task: ClaimedTask) -> None:
        """Stage inputs → run handler under heartbeat + cancel guard → publish.

        A per-task watchdog (when ``timeout`` > 0) enforces a wall-clock
        deadline off the event loop. Terminal reporting goes through a single
        TerminalGuard so a timeout and a near-simultaneous normal completion
        report exactly once (first resolver wins).
        """
        task_dir = self.work_dir / f"task_{task.id}"
        progress = ProgressReporter(
            self._client, task.id,
            heartbeat_interval_s=self.heartbeat_interval_s,
        )

        timeout_s = resolve_task_timeout(
            task.task_type,
            default_s=self.task_timeout_s,
            per_type=self.task_timeouts,
            env=self._timeout_env,
        )
        guard = TerminalGuard()
        wd = None
        if timeout_s > 0:
            log.info("task %s: %s timeout=%.0fs", task.id, task.task_type.value, timeout_s)
            wd = self._watchdog_factory(
                timeout_s=timeout_s,
                grace_s=self.timeout_grace_s,
                guard=guard,
                sync_fail=_make_sync_fail(self.backend_url, self.api_key, task.id),
                on_hard_exit=self._on_hard_exit,
                children_before=list_descendants(os.getpid()),
            )
            wd.start()

        outcome: tuple[str, object] = ("fail", "unknown")
        try:
            self._payload_logger.record(task)

            handler = self.handlers.get(task.task_type)
            if handler is None:
                raise ProtocolError(
                    f"no handler registered for task_type {task.task_type.value}"
                )
            params_schema = TASK_PARAMS_SCHEMAS.get(task.task_type)
            if params_schema is None:
                raise ProtocolError(
                    f"no schema registered for task_type {task.task_type.value}; "
                    "update task-worker-api or register one locally"
                )
            try:
                typed_params = params_schema(**task.params)
            except Exception as e:  # noqa: BLE001
                raise TaskParamsError(
                    f"task.params failed schema validation on claim: {e}"
                ) from e

            file_ctx = await prepare_inputs(task, self._client, task_dir)
            ctx = TaskContext(task=task, files=file_ctx, progress=progress)

            await progress.start_heartbeat()
            async with CancelGuard(
                self._client, task.id,
                poll_interval_s=self.cancel_poll_interval_s,
            ):
                result = await handler(ctx, typed_params)

            output_files = (result or {}).get("output_files") or {}
            if output_files:
                delivered = await upload_outputs(
                    task, self._client, file_ctx, output_files,
                    self.shared_volume_path,
                )
                result = {**result, "output_files": delivered}
            outcome = ("complete", result or {})

        except TaskCancelled:
            outcome = ("fail", "cancelled by user")
        except (TaskParamsError, ProtocolError) as e:
            log.error("task %s protocol error: %s", task.id, e)
            outcome = ("fail", str(e))
        except Exception as e:  # noqa: BLE001
            tb = traceback.format_exc()
            log.error("task %s failed: %s\n%s", task.id, e, tb)
            outcome = ("fail", f"{type(e).__name__}: {e}\n{tb}")
        finally:
            fired = wd.stop() if wd is not None else False
            await progress.stop()
            # Single terminal report. If the watchdog fired, the deadline won;
            # otherwise report the handler outcome. The guard makes this
            # exactly-once even against the watchdog's hard-exit path.
            if guard.claim():
                try:
                    if fired:
                        await self._client.fail(
                            task.id, f"timeout: exceeded {timeout_s:.0f}s",
                        )
                        log.warning("task %s timed out (%s)", task.id, task.task_type.value)
                    elif outcome[0] == "complete":
                        await self._client.complete(task.id, outcome[1])
                        log.info("task %s completed (%s)", task.id, task.task_type.value)
                    else:
                        await self._client.fail(task.id, outcome[1])
                        if outcome[1] == "cancelled by user":
                            log.info("task %s cancelled by user", task.id)
                except Exception:  # noqa: BLE001
                    pass
            shutil.rmtree(task_dir, ignore_errors=True)
```

- [ ] **Step 5: Run the full suite**

Run: `pytest -q`
Expected: PASS — all new `test_worker_timeout.py` tests plus the existing 52 (the existing `test_worker_loop.py` cases still pass because the default `task_timeout_s=1800` never fires in those fast tests, and terminal reporting is unchanged in outcome).

- [ ] **Step 6: Commit**

```bash
git add src/task_worker_api/worker.py tests/test_worker_timeout.py
git commit -m "feat(timeout): enforce per-task timeout in Worker via watchdog"
```

---

## Task 7: Version bump + CHANGELOG

**Files:**
- Modify: `pyproject.toml:8`
- Modify: `CHANGELOG.md` (top)

- [ ] **Step 1: Bump the version**

In `pyproject.toml` change:

```toml
version = "0.6.1"
```
to:
```toml
version = "0.7.0"
```

- [ ] **Step 2: Add the CHANGELOG entry**

Prepend under the top heading of `CHANGELOG.md` (match the file's existing entry style):

```markdown
## 0.7.0

### Added
- Per-task execution timeout. `Worker` now enforces a wall-clock deadline per
  task (default 30 min) via an OS watchdog thread that fires even when a handler
  blocks the event loop. On expiry it SIGTERM→SIGKILLs the task-spawned child
  processes and fails the task terminally (`timeout: exceeded Ns`); an in-process
  wedge with nothing to kill triggers a bounded sync fail + process exit (the
  container restart policy recovers).
- Config: constructor `task_timeout_s` / `task_timeouts={TaskType: seconds}` /
  `timeout_grace_s` / `on_hard_exit`, plus env `WORKER_TASK_TIMEOUTS="default=1800,gs_build=7200"`.
  A resolved value `<= 0` disables the timeout for that task type.

### Notes
- Known limitations (see the design spec): a pure-Python GIL-holding busy loop
  can starve the watchdog thread; child attribution in `run_hybrid` workers is
  snapshot-delta based. Process-per-task is the documented future hardening.
```

- [ ] **Step 3: Run the suite once more**

Run: `pytest -q`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml CHANGELOG.md
git commit -m "chore: release task-worker-api 0.7.0 (per-task timeout)"
```

---

## Rollout (post-merge — other repos, NOT part of this plan's commits)

These land after `0.7.0` publishes (CI release workflow on merge to main). Track but do not implement here:

1. **Bump the SDK pin** in each worker repo to `task-worker-api==0.7.0` per [`docs/fleet/runbooks/sdk-upgrade.md`](../../fleet/runbooks/sdk-upgrade.md): Blender-CLI, colmap-splat, Neural-Canvas, syngar-ml-assetbundle-builder.
2. **Set `WORKER_TASK_TIMEOUTS`** per worker service in `surgiclaw-deploy/bundle/compose/docker-compose.yml`. At minimum give `colmap-splat-worker` a `gs_build` override (e.g. `default=1800,gs_build=7200`); leave blender/neural-canvas/assetbundle on the 1800 default unless a type is known-long.
3. **Publish a new prod bundle + deploy** (`surgiclaw publish --channel prod` → `surgiclaw deploy prod`) so the timeouts take effect on bm03/bm04.

---

## Self-Review

**Spec coverage:** default+per-type config (Task 1) ✓; watchdog-thread off the loop (Tasks 2-4) ✓; task-spawned-only kill via snapshot delta + pid/create_time (Task 3-4) ✓; SIGTERM→SIGKILL→hard-exit ladder (Task 4) ✓; bounded sync fail, never touches async client (Task 5) ✓; exactly-once first-wins guard + deadline-covers-staging via start-at-claim (Task 6) ✓; injectable `on_hard_exit` for testability (Tasks 4-6) ✓; disabled-on-`<=0` (Tasks 1,6) ✓; version/CHANGELOG (Task 7) ✓; rollout documented ✓.

**Placeholder scan:** none — every code/test step has complete content.

**Type consistency:** `ProcId`, `TerminalGuard.claim()`, `list_descendants(pid)`, `kill_procs(procs, sig)`, `TaskWatchdog(timeout_s=, grace_s=, guard=, sync_fail=, on_hard_exit=, children_before=, ...)`, `_make_sync_fail(base_url, api_key, task_id, *, timeout_s)`, and the `_watchdog_factory(**kwargs)` keyword contract are consistent across the watchdog impl, the `Worker` call site, and the `_ImmediateWatchdog` test double (same kwargs).
