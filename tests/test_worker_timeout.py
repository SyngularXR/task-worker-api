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


@pytest.mark.asyncio
async def test_timeout_after_publish_discards_the_staging_dir(
    make_worker, fake_client, tmp_path,
):
    """A watchdog deadline that lands after the handler published its outputs
    fails the task — and the published artifacts must go with it. The backend
    only sweeps a staging dir for a task it recorded as *complete*, so leaving
    them behind orphans GB-scale artifacts on the shared volume for good."""
    _queue(fake_client, tmp_path)
    shared = tmp_path / "shared"

    async def handler(ctx, params):
        (ctx.files.output_dir / "planes.stl").write_bytes(b"cut-planes")
        return {"output_files": {"planes": "planes.stl"}}

    w = make_worker(
        client=fake_client,
        handlers={TaskType.DETECT_CUT_PLANES: handler},
        task_timeout_s=60.0,
        shared_volume_path=str(shared),
        _watchdog_factory=lambda **kw: _ImmediateWatchdog(fire=True),
    )
    await w.run_one()

    assert fake_client.completed_tasks == []
    assert "timeout: exceeded 60s" in fake_client.failed_tasks[0]["error"]
    assert not (shared / "temp" / "1").exists()


@pytest.mark.asyncio
async def test_timeout_reclaim_spares_a_requeued_attempts_outputs(
    make_worker, fake_client, tmp_path, monkeypatch,
):
    """The watchdog's report can hand the task to a successor before this
    attempt's cleanup runs.

    ``_make_sync_fail`` reports the timeout from the watchdog thread while
    the event loop is still wedged, so the backend can re-queue the task and
    a second attempt can stage fresh outputs into the same
    ``temp/<task_id>`` — the shared staging path — before this attempt
    resumes and reclaims. It must reclaim only what it can still prove is
    its own.
    """
    _queue(fake_client, tmp_path)
    shared = tmp_path / "shared"
    staged_path = shared / "temp" / "1" / "planes.stl"

    async def handler(ctx, params):
        (ctx.files.output_dir / "planes.stl").write_bytes(b"cut-planes")
        return {"output_files": {"planes": "planes.stl"}}

    import task_worker_api.worker as worker_mod
    real_discard = worker_mod.discard_published_outputs

    async def discard_after_a_requeued_attempt_republished(*args, **kwargs):
        # Between this attempt's publish and its cleanup, the re-queued
        # attempt lands its own outputs at the same path.
        staged_path.write_bytes(b"second attempt's cut-planes")
        return await real_discard(*args, **kwargs)

    monkeypatch.setattr(
        worker_mod, "discard_published_outputs",
        discard_after_a_requeued_attempt_republished,
    )

    w = make_worker(
        client=fake_client,
        handlers={TaskType.DETECT_CUT_PLANES: handler},
        task_timeout_s=60.0,
        shared_volume_path=str(shared),
        _watchdog_factory=lambda **kw: _ImmediateWatchdog(fire=True),
    )
    await w.run_one()

    assert "timeout: exceeded 60s" in fake_client.failed_tasks[0]["error"]
    assert staged_path.read_bytes() == b"second attempt's cut-planes"


@pytest.mark.asyncio
async def test_completed_task_keeps_its_staging_dir(
    make_worker, fake_client, tmp_path,
):
    """The mirror image, and the regression this cleanup must never become:
    a task that completes hands its staging dir to the backend to sweep."""
    _queue(fake_client, tmp_path)
    shared = tmp_path / "shared"

    async def handler(ctx, params):
        (ctx.files.output_dir / "planes.stl").write_bytes(b"cut-planes")
        return {"output_files": {"planes": "planes.stl"}}

    w = make_worker(
        client=fake_client,
        handlers={TaskType.DETECT_CUT_PLANES: handler},
        task_timeout_s=60.0,
        shared_volume_path=str(shared),
        _watchdog_factory=lambda **kw: _ImmediateWatchdog(fire=False),
    )
    await w.run_one()

    assert len(fake_client.completed_tasks) == 1
    assert (shared / "temp" / "1" / "planes.stl").read_bytes() == b"cut-planes"


class _WedgedLoopWatchdog:
    """Test double for the watchdog's phase-3 in-process wedge.

    Phases 1 and 2 kill the task's children; when neither frees the event
    loop, the watchdog claims the terminal report and sends the timeout
    ``fail`` itself from its own thread (``TaskWatchdog._run``). The loop can
    unwedge afterwards — the hard exit is not instantaneous, and consumers can
    inject a non-exiting ``on_hard_exit`` — and when it does it finds the
    report already gone. Claiming in ``stop()`` puts the claim exactly where
    the real one lands relative to the loop: before ``_run_one`` reaches its
    terminal block.
    """

    def __init__(self, *, guard, **_kw):
        self._guard = guard
        self.fired = False
        self.won_report = False

    def start(self):
        pass

    def stop(self) -> bool:
        self.fired = True
        self.won_report = self._guard.claim()
        return self.fired


@pytest.mark.asyncio
async def test_watchdog_winning_the_report_still_discards_published_outputs(
    make_worker, fake_client, tmp_path,
):
    """Cleanup must not hang off winning the terminal report.

    The watchdog reports the timeout while the loop is wedged, so the resumed
    ``_run_one`` never sends a report of its own — but it is still the only
    thing that knows what this attempt staged onto the shared volume, and the
    backend only sweeps a staging dir for a task it recorded as *complete*.
    Skipping the discard here leaves exactly the post-publish orphan the
    cleanup exists to prevent.
    """
    _queue(fake_client, tmp_path)
    shared = tmp_path / "shared"

    async def handler(ctx, params):
        (ctx.files.output_dir / "planes.stl").write_bytes(b"cut-planes")
        return {"output_files": {"planes": "planes.stl"}}

    built = []

    def factory(**kw):
        built.append(_WedgedLoopWatchdog(**kw))
        return built[-1]

    w = make_worker(
        client=fake_client,
        handlers={TaskType.DETECT_CUT_PLANES: handler},
        task_timeout_s=60.0,
        shared_volume_path=str(shared),
        _watchdog_factory=factory,
    )
    await w.run_one()

    # The watchdog owned the report: the loop sent neither terminal status.
    assert built[0].won_report is True
    assert fake_client.completed_tasks == []
    assert fake_client.failed_tasks == []
    # ...and the outputs it published are still reclaimed.
    assert not (shared / "temp" / "1").exists()
