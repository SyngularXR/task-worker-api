from __future__ import annotations

import json
import os

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
async def test_timeout_reclaim_spares_a_requeued_attempts_staging_dir(
    make_worker, fake_client, tmp_path, monkeypatch,
):
    """The watchdog's report can hand the task to a successor before this
    attempt's cleanup runs.

    ``_make_sync_fail`` reports the timeout from the watchdog thread while
    the event loop is still wedged, so the backend can re-queue the task and
    a second attempt can be staging its own outputs under ``temp/<task_id>``
    before this attempt resumes and reclaims. The successor is modelled at
    its most fragile moment — directory created, first copy not yet landed —
    because that is the state an "is it empty?" cleanup would delete.
    """
    _queue(fake_client, tmp_path)
    shared = tmp_path / "shared"
    successor = shared / "temp" / "1" / "the-next-attempt"

    async def handler(ctx, params):
        (ctx.files.output_dir / "planes.stl").write_bytes(b"cut-planes")
        return {"output_files": {"planes": "planes.stl"}}

    import task_worker_api.worker as worker_mod
    real_discard = worker_mod.discard_published_outputs
    mine: list = []

    async def discard_after_a_requeued_attempt_started_publishing(*a, **kw):
        # Snapshot what this attempt staged, then let the re-queued attempt
        # claim the task and create its own staging dir underneath.
        mine.extend((shared / "temp" / "1").iterdir())
        successor.mkdir(parents=True)
        return await real_discard(*a, **kw)

    monkeypatch.setattr(
        worker_mod, "discard_published_outputs",
        discard_after_a_requeued_attempt_started_publishing,
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
    # This attempt's outputs are gone...
    assert mine and not any(d.exists() for d in mine)
    # ...and the successor's staging dir came through untouched.
    assert successor.is_dir()


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
    (staging,) = list((shared / "temp" / "1").iterdir())
    assert (staging / "planes.stl").read_bytes() == b"cut-planes"
    # And the manifest points the backend at exactly that path.
    published = fake_client.completed_tasks[0]["result"]["output_files"]
    assert published == {"planes": str(staging / "planes.stl")}


@pytest.mark.asyncio
async def test_completed_task_staging_dir_goes_once_the_mirror_empties_it(
    make_worker, fake_client, tmp_path, monkeypatch,
):
    """The SDK clears up after itself on the success path.

    The backend's post-complete mirror moves the artifacts to their permanent
    home and then rmdirs ``temp/<task_id>`` — but not recursively, so the
    emptied per-attempt dir underneath would keep the task dir alive and
    leave one directory per completed task on the shared volume. The prune is
    ``rmdir``-only, which is why the test above (mirror hasn't run, files
    still there) keeps its staging dir and this one doesn't.
    """
    _queue(fake_client, tmp_path)
    shared = tmp_path / "shared"

    async def handler(ctx, params):
        (ctx.files.output_dir / "planes.stl").write_bytes(b"cut-planes")
        return {"output_files": {"planes": "planes.stl"}}

    real_complete = fake_client.complete

    async def complete_then_mirror(task_id, result):
        await real_complete(task_id, result)
        # Stand in for the backend hook moving the artifacts out.
        for path in result["output_files"].values():
            os.unlink(path)

    monkeypatch.setattr(fake_client, "complete", complete_then_mirror)

    w = make_worker(
        client=fake_client,
        handlers={TaskType.DETECT_CUT_PLANES: handler},
        task_timeout_s=60.0,
        shared_volume_path=str(shared),
        _watchdog_factory=lambda **kw: _ImmediateWatchdog(fire=False),
    )
    await w.run_one()

    assert len(fake_client.completed_tasks) == 1
    assert not (shared / "temp" / "1").exists()
