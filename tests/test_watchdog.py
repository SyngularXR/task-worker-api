from __future__ import annotations

import os
import subprocess
import sys
import threading
import time as _time

import pytest

from task_worker_api.watchdog import (
    TaskWatchdog,
    TerminalGuard,
    _SIGKILL,
    _SIGTERM,
    kill_procs,
    list_descendants,
)

_LINUX = sys.platform.startswith("linux")


# ----- TerminalGuard --------------------------------------------------------


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


# ----- /proc enumeration + kill --------------------------------------------


@pytest.mark.skipif(not _LINUX, reason="/proc enumeration is Linux-only")
def test_list_descendants_finds_child_then_kill_removes_it():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        descendants = list_descendants(os.getpid())
        pids = {pid for pid, _ in descendants}
        assert proc.pid in pids

        ours = {(pid, ct) for pid, ct in descendants if pid == proc.pid}
        kill_procs(ours, _SIGKILL)
        proc.wait(timeout=5)
        assert proc.returncode is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


@pytest.mark.skipif(not _LINUX, reason="/proc enumeration is Linux-only")
def test_kill_procs_skips_reused_pid():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        kill_procs({(proc.pid, "0")}, _SIGKILL)  # bogus create_time
        _time.sleep(0.3)
        assert proc.poll() is None  # still alive — we refused to kill it
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_list_descendants_empty_off_proc(monkeypatch):
    monkeypatch.setattr("task_worker_api.watchdog.os.path.isdir", lambda p: False)
    assert list_descendants(1) == set()


# ----- TaskWatchdog escalation ladder (real time, tiny durations) ----------
#
# The watchdog runs in a real OS thread, so these tests use small real
# durations and observe the *sequence* of actions. A fake clock can't be used
# here: instant virtual time lets the thread race through the whole timeline
# before the test can interact with it.


def _make_wd(*, timeout_s, grace_s, list_fn, kill_calls, guard=None,
             hard_exit_calls=None, sync_fail_calls=None, children_before=None):
    return TaskWatchdog(
        timeout_s=timeout_s,
        grace_s=grace_s,
        guard=guard or TerminalGuard(),
        sync_fail=lambda err: (sync_fail_calls if sync_fail_calls is not None else []).append(err),
        on_hard_exit=lambda: (hard_exit_calls if hard_exit_calls is not None else []).append(True),
        children_before=children_before or set(),
        list_descendants_fn=list_fn,
        kill_fn=lambda procs, sig: kill_calls.append((set(procs), sig)),
        tick_s=0.01,
        worker_pid=1234,
    )


def test_no_fire_when_stopped_before_deadline():
    kill_calls = []
    wd = _make_wd(timeout_s=2.0, grace_s=2.0,
                  list_fn=lambda pid: set(), kill_calls=kill_calls)
    wd.start()
    wd.stop()              # signal completion immediately
    wd._thread.join(timeout=5)
    assert wd.fired is False
    assert kill_calls == []


def test_fires_and_kills_task_spawned_children():
    kill_calls, hard_exit = [], []
    before = {(10, "100")}
    after = {(10, "100"), (11, "200")}   # pid 11 spawned by the task
    wd = _make_wd(timeout_s=0.05, grace_s=5.0,
                  list_fn=lambda pid: after, kill_calls=kill_calls,
                  hard_exit_calls=hard_exit, children_before=before)
    wd.start()
    # Wait for the deadline to fire + SIGTERM to be issued, then signal the
    # loop resumed (handler unblocked) so it stops before SIGKILL / hard-exit.
    for _ in range(300):
        if kill_calls:
            break
        _time.sleep(0.01)
    wd.stop()
    wd._thread.join(timeout=5)
    assert wd.fired is True
    assert hard_exit == []
    assert kill_calls[0][0] == {(11, "200")}        # only the new child
    assert kill_calls[0][1] == _SIGTERM


def test_hard_exit_when_nothing_killable_and_loop_never_resumes():
    kill_calls, hard_exit, sync_fail = [], [], []
    guard = TerminalGuard()
    wd = _make_wd(timeout_s=0.05, grace_s=0.02,
                  list_fn=lambda pid: set(),     # no descendants at all
                  kill_calls=kill_calls, guard=guard,
                  hard_exit_calls=hard_exit, sync_fail_calls=sync_fail)
    wd.start()
    wd._thread.join(timeout=5)   # never stopped → runs the full ladder
    assert wd.fired is True
    assert len(hard_exit) == 1
    assert len(sync_fail) == 1
    assert "timeout" in sync_fail[0]
    assert guard.claim() is False   # watchdog already claimed the report


def test_hard_exit_skips_sync_fail_if_guard_already_claimed():
    hard_exit, sync_fail = [], []
    guard = TerminalGuard()
    assert guard.claim() is True   # loop already reported
    wd = _make_wd(timeout_s=0.05, grace_s=0.02, list_fn=lambda pid: set(),
                  kill_calls=[], guard=guard,
                  hard_exit_calls=hard_exit, sync_fail_calls=sync_fail)
    wd.start()
    wd._thread.join(timeout=5)
    assert sync_fail == []          # guard was taken; no duplicate report
    assert len(hard_exit) == 1      # still hard-exits to recover the worker
