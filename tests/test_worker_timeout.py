from __future__ import annotations

import json

import pytest

from task_worker_api import TaskType, Worker
from task_worker_api.worker import _make_sync_fail


# ----- bounded synchronous fail helper -------------------------------------


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

    fn = _make_sync_fail(
        "http://bk/api/v1/", "secret", 42, "worker / 1", timeout_s=3.0
    )
    fn("timeout: exceeded 1800s (hard-exit)")

    assert captured["url"] == (
        "http://bk/api/v1/tasks/42/fail?worker_id=worker%20%2F%201"
    )
    assert captured["method"] == "PUT"
    assert captured["timeout"] == 3.0
    assert captured["auth"] == "Bearer secret"
    assert captured["body"] == {"error": "timeout: exceeded 1800s (hard-exit)"}
    assert captured["closed"] is True


# ----- constructor validation ----------------------------------------------


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_task_timeout_s_rejected(make_worker, fake_client, bad):
    """NaN fails _run_one's `timeout_s > 0` check (watchdog never starts) and
    inf never fires — either way a wedged handler runs unbounded with no log
    line saying why. Reject at construction like the pacing knobs."""
    with pytest.raises(ValueError, match="task_timeout_s"):
        make_worker(client=fake_client, task_timeout_s=bad)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_task_timeouts_value_rejected(make_worker, fake_client, bad):
    with pytest.raises(ValueError, match="task_timeouts"):
        make_worker(
            client=fake_client,
            task_timeouts={TaskType.DETECT_CUT_PLANES: bad},
        )


def test_zero_and_negative_task_timeouts_still_allowed(make_worker, fake_client):
    """<= 0 is the documented "no timeout" escape hatch — only non-finite is
    rejected. Ints stay usable and are coerced to float."""
    w = make_worker(
        client=fake_client,
        task_timeout_s=0.0,
        task_timeouts={TaskType.DETECT_CUT_PLANES: -1, TaskType.GS_BUILD: 60},
    )
    assert w.task_timeout_s == 0.0
    assert w.task_timeouts == {
        TaskType.DETECT_CUT_PLANES: -1.0, TaskType.GS_BUILD: 60.0,
    }
    assert all(type(v) is float for v in w.task_timeouts.values())


# ----- Worker wiring (deterministic watchdog double) -----------------------


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


def _queue(fake, tmp_path):
    (tmp_path / "f.stl").write_bytes(b"solid\nendsolid\n")
    fake.queue_task(
        task_type=TaskType.DETECT_CUT_PLANES,
        params={"input_path": str(tmp_path / "f.stl")},
    )


@pytest.mark.asyncio
async def test_timeout_disabled_no_watchdog(make_worker, fake_client, tmp_path):
    _queue(fake_client, tmp_path)
    made = {"n": 0}

    def factory(**kwargs):
        made["n"] += 1
        return _ImmediateWatchdog(fire=False)

    async def handler(ctx, params):
        return {"ok": True}

    w = make_worker(
        client=fake_client,
        handlers={TaskType.DETECT_CUT_PLANES: handler},
        task_timeout_s=0.0,
        _watchdog_factory=factory,
    )
    await w.run_one()
    assert made["n"] == 0                      # disabled → no watchdog built
    assert len(fake_client.completed_tasks) == 1
    assert fake_client.failed_tasks == []


@pytest.mark.asyncio
async def test_timeout_fires_reports_timeout_fail(make_worker, fake_client, tmp_path):
    _queue(fake_client, tmp_path)

    async def handler(ctx, params):
        return {"ok": True}   # completes, but the watchdog reports it fired

    w = make_worker(
        client=fake_client,
        handlers={TaskType.DETECT_CUT_PLANES: handler},
        task_timeout_s=60.0,
        _watchdog_factory=lambda **kw: _ImmediateWatchdog(fire=True),
    )
    await w.run_one()
    assert fake_client.completed_tasks == []
    assert len(fake_client.failed_tasks) == 1
    assert "timeout: exceeded 60s" in fake_client.failed_tasks[0]["error"]
    assert _ImmediateWatchdog.last.started is True
    assert _ImmediateWatchdog.last.stopped is True


@pytest.mark.asyncio
async def test_no_timeout_normal_completion(make_worker, fake_client, tmp_path):
    _queue(fake_client, tmp_path)

    async def handler(ctx, params):
        return {"ok": True}

    w = make_worker(
        client=fake_client,
        handlers={TaskType.DETECT_CUT_PLANES: handler},
        task_timeout_s=60.0,
        _watchdog_factory=lambda **kw: _ImmediateWatchdog(fire=False),
    )
    await w.run_one()
    assert len(fake_client.completed_tasks) == 1
    assert fake_client.failed_tasks == []


@pytest.mark.asyncio
async def test_per_type_env_override_resolved(
    make_worker, fake_client, tmp_path, monkeypatch,
):
    monkeypatch.setenv("WORKER_TASK_TIMEOUTS", "default=10,detect_cut_planes=0")
    _queue(fake_client, tmp_path)
    built = {"n": 0}

    def factory(**kwargs):
        built["n"] += 1
        return _ImmediateWatchdog(fire=False)

    async def handler(ctx, params):
        return {"ok": True}

    # detect_cut_planes resolves to 0 (disabled) → factory never called.
    w = make_worker(
        client=fake_client,
        handlers={TaskType.DETECT_CUT_PLANES: handler},
        _watchdog_factory=factory,
    )
    await w.run_one()
    assert built["n"] == 0
    assert len(fake_client.completed_tasks) == 1
