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


# ----- Phase 2 escalation (SIGKILL survivors) -------------------------------
#
# When SIGTERM doesn't free the loop within the grace window, the watchdog
# re-enumerates and SIGKILLs the survivors before escalating to hard-exit.


def test_sigkill_phase_when_sigterm_does_not_free_loop():
    kill_calls, hard_exit = [], []
    before = set()
    after = {(11, "200")}
    wd = _make_wd(timeout_s=0.05, grace_s=0.02,
                  list_fn=lambda pid: after, kill_calls=kill_calls,
                  hard_exit_calls=hard_exit, children_before=before)
    wd.start()
    wd._thread.join(timeout=5)   # never stopped → full ladder

    assert wd.fired is True
    assert len(kill_calls) == 2
    assert kill_calls[0][1] == _SIGTERM
    assert kill_calls[1][1] == _SIGKILL
    assert kill_calls[0][0] == {(11, "200")}
    assert kill_calls[1][0] == {(11, "200")}
    assert len(hard_exit) == 1   # still hard-exits after SIGKILL


def test_sync_fail_exception_does_not_block_hard_exit(caplog):
    """If the injected sync_fail raises, the watchdog must log it and still
    call on_hard_exit — otherwise the worker is stranded."""
    hard_exit = []

    def _boom_fail(err):
        raise RuntimeError("fail channel broken")

    wd = TaskWatchdog(
        timeout_s=0.05, grace_s=0.02,
        guard=TerminalGuard(),
        sync_fail=_boom_fail,
        on_hard_exit=lambda: hard_exit.append(True),
        children_before=set(),
        list_descendants_fn=lambda pid: set(),
        kill_fn=lambda procs, sig: None,
        tick_s=0.01,
        worker_pid=1234,
    )
    with caplog.at_level("WARNING"):
        wd.start()
        wd._thread.join(timeout=5)

    assert wd.fired is True
    assert len(hard_exit) == 1
    assert any("sync_fail failed" in r.message for r in caplog.records)


# ----- _read_stat / list_descendants / kill_procs unit tests ----------------
#
# These exercise the /proc parsing logic without a real Linux process tree,
# by monkeypatching open / os.listdir / os.kill.


def test_read_stat_parses_ppid_and_starttime(monkeypatch):
    """A well-formed /proc/<pid>/stat line must yield (ppid, starttime)."""
    from task_worker_api import watchdog

    # field 1 = pid (already known), field 2 = comm (can have spaces/parens),
    # field 3 = state, field 4 = ppid ... field 22 = starttime.
    # We craft a line where comm contains a closing paren + spaces to verify
    # the rfind(")") split logic.
    fake_line = "42 (my (proc) name) S 1 0 0 0 -1 4194304 100 0 0 0 1 2 0 0 20 0 1 0 9876543 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0"

    import builtins
    real_open = builtins.open

    class _FakeFile:
        def __init__(self, text):
            self._text = text

        def read(self):
            return self._text

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    def _fake_open(path, *args, **kwargs):
        if isinstance(path, str) and path.startswith("/proc/") and path.endswith("/stat"):
            return _FakeFile(fake_line)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _fake_open)
    result = watchdog._read_stat(42)
    assert result == (1, "9876543")


def test_read_stat_returns_none_on_oserror(monkeypatch):
    """A missing or unreadable /proc/<pid>/stat must return None, not raise."""
    from task_worker_api import watchdog

    import builtins

    def _fake_open(path, *args, **kwargs):
        raise OSError("no such file")

    monkeypatch.setattr(builtins, "open", _fake_open)
    assert watchdog._read_stat(999) is None


def test_read_stat_returns_none_on_malformed_line(monkeypatch):
    """A line with no closing paren must return None, not crash."""
    from task_worker_api import watchdog

    import builtins

    class _FakeFile:
        def __init__(self, text):
            self._text = text

        def read(self):
            return self._text

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    def _fake_open(path, *args, **kwargs):
        return _FakeFile("no parens here at all")

    monkeypatch.setattr(builtins, "open", _fake_open)
    assert watchdog._read_stat(1) is None


def test_list_descendants_walks_proc_tree(monkeypatch):
    """list_descendants builds a parent→child map from /proc and returns the
    full descendant set of the given pid."""
    from task_worker_api import watchdog

    # Simulate: pid 1 (init) → pid 100 → pid 101, 102.
    # _read_stat returns (ppid, starttime).
    stats = {
        1: (0, "1"),
        100: (1, "100"),
        101: (100, "101"),
        102: (100, "102"),
        200: (1, "200"),  # sibling of 100, not a descendant of 100
    }
    monkeypatch.setattr(watchdog.os.path, "isdir", lambda p: p == "/proc")
    monkeypatch.setattr(watchdog.os, "listdir", lambda d: ["1", "100", "101", "102", "200"])
    monkeypatch.setattr(watchdog, "_read_stat", lambda pid: stats.get(pid))

    descendants = watchdog.list_descendants(100)
    pids = {pid for pid, _ in descendants}
    assert pids == {101, 102}


def test_list_descendants_empty_when_no_children(monkeypatch):
    from task_worker_api import watchdog

    stats = {1: (0, "1"), 50: (1, "50")}
    monkeypatch.setattr(watchdog.os.path, "isdir", lambda p: p == "/proc")
    monkeypatch.setattr(watchdog.os, "listdir", lambda d: ["1", "50"])
    monkeypatch.setattr(watchdog, "_read_stat", lambda pid: stats.get(pid))

    assert watchdog.list_descendants(999) == set()


def test_list_descendants_returns_empty_on_listdir_oserror(monkeypatch):
    """If os.listdir('/proc') raises, return an empty set (best-effort)."""
    from task_worker_api import watchdog

    monkeypatch.setattr(watchdog.os.path, "isdir", lambda p: p == "/proc")
    monkeypatch.setattr(watchdog.os, "listdir", lambda d: (_ for _ in ()).throw(OSError("denied")))

    assert watchdog.list_descendants(1) == set()


def test_kill_procs_swallows_oserror(monkeypatch):
    """os.kill raising OSError (process already exited) must be swallowed."""
    from task_worker_api import watchdog

    killed = []

    def _fake_kill(pid, sig):
        killed.append((pid, sig))
        raise OSError("no such process")

    # Make _read_stat return a matching starttime so the pid isn't skipped.
    monkeypatch.setattr(watchdog, "_read_stat", lambda pid: (1, "999"))
    monkeypatch.setattr(watchdog.os, "kill", _fake_kill)

    # Should not raise.
    watchdog.kill_procs({(42, "999")}, _SIGTERM)
    assert killed == [(42, _SIGTERM)]


def test_kill_procs_skips_pid_with_mismatched_starttime(monkeypatch):
    """If the current starttime doesn't match (PID was reused), skip the kill."""
    from task_worker_api import watchdog

    killed = []
    monkeypatch.setattr(watchdog, "_read_stat", lambda pid: (1, "different_time"))
    monkeypatch.setattr(watchdog.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    watchdog.kill_procs({(42, "old_time")}, _SIGTERM)
    assert killed == []  # skipped — starttime mismatch


def test_kill_procs_skips_missing_pid(monkeypatch):
    """If _read_stat returns None (process gone), skip the kill."""
    from task_worker_api import watchdog

    killed = []
    monkeypatch.setattr(watchdog, "_read_stat", lambda pid: None)
    monkeypatch.setattr(watchdog.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    watchdog.kill_procs({(42, "999")}, _SIGTERM)
    assert killed == []


# ----- _make_sync_fail retry ------------------------------------------------
# The watchdog's last-resort reporter is a sync urllib PUT — the only report
# path for a watchdog-fired timeout. A single attempt against a momentarily
# unavailable backend (restart, DB blip) silently orphaned the task as
# RUNNING until the sweeper; it now retries a few times with a short sleep.


def _fake_response():
    class _Resp:
        def close(self):
            pass

    return _Resp()


def test_sync_fail_retries_then_succeeds(monkeypatch):
    from task_worker_api import worker as worker_mod

    calls = {"n": 0}
    sleeps: list[float] = []

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError("connection refused")
        return _fake_response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(worker_mod.time, "sleep", lambda s: sleeps.append(s))

    sync_fail = worker_mod._make_sync_fail(
        "http://fake/api/v1", "key", 691, "worker-1"
    )
    sync_fail("timeout: exceeded 1800s")

    assert calls["n"] == 3
    # A sleep between each failed attempt, none after success.
    assert sleeps == [2.0, 2.0]


def test_sync_fail_raises_after_exhausting_attempts(monkeypatch):
    from task_worker_api import worker as worker_mod

    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        raise OSError("still down")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(worker_mod.time, "sleep", lambda s: None)

    sync_fail = worker_mod._make_sync_fail(
        "http://fake/api/v1", "key", 691, "worker-1"
    )
    with pytest.raises(OSError, match="still down"):
        sync_fail("timeout")

    assert calls["n"] == 3


def test_sync_fail_no_sleep_on_first_success(monkeypatch):
    from task_worker_api import worker as worker_mod

    calls = {"n": 0}
    sleeps: list[float] = []

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        return _fake_response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(worker_mod.time, "sleep", lambda s: sleeps.append(s))

    sync_fail = worker_mod._make_sync_fail(
        "http://fake/api/v1", "key", 5, "worker-1"
    )
    sync_fail("err")

    assert calls["n"] == 1
    assert sleeps == []
