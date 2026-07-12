"""Integration-style tests for Worker.run_one with FakeBackendClient.

Exercises the full claim → validate → handler → complete path without a
real HTTP backend. Covers happy path, schema rejection, handler failure,
and cooperative cancel.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from task_worker_api import (
    TaskCancelled,
    TaskContext,
    TaskType,
    Worker,
)
from task_worker_api.schemas import TASK_PARAMS_SCHEMAS, DetectCutPlanesParams
from task_worker_api.schemas._base import TaskParamsBase
from task_worker_api.testing import FakeBackendClient
from pydantic import ConfigDict


@pytest.mark.asyncio
async def test_worker_claims_runs_and_completes_happy_path(
    make_worker, fake_client, tmp_path,
):
    fake_client.queue_task(
        task_type=TaskType.DETECT_CUT_PLANES,
        params={
            # the STL doesn't need to exist: our handler returns a canned
            # payload without touching disk, and Worker.files puts it on
            # an empty input_dir when input_path points at a missing file.
            # So we avoid prepare_inputs by using a *real* tmp file.
        },
    )
    # Rewrite: add an actual input_path the prepare_inputs can read.
    fake_client._queue[0] = fake_client._queue[0].__class__(
        id=fake_client._queue[0].id,
        task_type=fake_client._queue[0].task_type,
        case_id=fake_client._queue[0].case_id,
        item_key=fake_client._queue[0].item_key,
        status=fake_client._queue[0].status,
        params={"input_path": str(tmp_path / "fake.stl"), "max_results": 3},
        worker_id=None,
    )
    (tmp_path / "fake.stl").write_bytes(b"solid empty\nendsolid empty\n")

    received: dict = {}

    async def handler(ctx: TaskContext, params: DetectCutPlanesParams) -> dict:
        received["max_results"] = params.max_results
        received["primary_name"] = ctx.files.primary_path.name
        return {"planes": [{"rank": 0}], "stats": {}}

    worker = make_worker(
        client=fake_client,
        handlers={TaskType.DETECT_CUT_PLANES: handler},
    )
    ran = await worker.run_one()
    assert ran is True

    assert received["max_results"] == 3
    assert received["primary_name"] == "fake.stl"
    assert len(fake_client.completed_tasks) == 1
    assert fake_client.completed_tasks[0]["result"] == {"planes": [{"rank": 0}], "stats": {}}
    assert fake_client.failed_tasks == []


@pytest.mark.asyncio
async def test_worker_rejects_params_with_extra_fields(
    make_worker, fake_client, tmp_path,
):
    """extra='forbid' on the schema → task fails with TaskParamsError."""
    (tmp_path / "fake.stl").write_bytes(b"solid\nendsolid\n")
    fake_client.queue_task(
        task_type=TaskType.DETECT_CUT_PLANES,
        params={
            "input_path": str(tmp_path / "fake.stl"),
            "input_file": "oops",  # spelled wrong — extra="forbid" rejects
        },
    )

    async def handler(ctx, params):  # should never be called
        raise AssertionError("handler must not run on invalid params")

    worker = make_worker(
        client=fake_client,
        handlers={TaskType.DETECT_CUT_PLANES: handler},
    )
    await worker.run_one()

    assert fake_client.completed_tasks == []
    assert len(fake_client.failed_tasks) == 1
    assert "failed schema validation" in fake_client.failed_tasks[0]["error"]


@pytest.mark.asyncio
async def test_worker_reports_handler_exception_as_fail(
    make_worker, fake_client, tmp_path,
):
    (tmp_path / "fake.stl").write_bytes(b"solid\nendsolid\n")
    fake_client.queue_task(
        task_type=TaskType.DETECT_CUT_PLANES,
        params={"input_path": str(tmp_path / "fake.stl")},
    )

    async def handler(ctx, params):
        raise RuntimeError("boom")

    worker = make_worker(
        client=fake_client,
        handlers={TaskType.DETECT_CUT_PLANES: handler},
    )
    await worker.run_one()

    assert fake_client.completed_tasks == []
    assert len(fake_client.failed_tasks) == 1
    assert "RuntimeError: boom" in fake_client.failed_tasks[0]["error"]


@pytest.mark.asyncio
async def test_worker_reports_cancel_as_fail_cooperative(
    make_worker, fake_client, tmp_path,
):
    (tmp_path / "fake.stl").write_bytes(b"solid\nendsolid\n")
    task = fake_client.queue_task(
        task_type=TaskType.DETECT_CUT_PLANES,
        params={"input_path": str(tmp_path / "fake.stl")},
    )
    fake_client.mark_cancelled(task.id)

    async def handler(ctx, params):
        # The handler never explicitly checks; the CancelGuard raises
        # TaskCancelled at the next await.
        import asyncio
        for _ in range(20):
            await asyncio.sleep(0.1)
        return {}  # pragma: no cover — should never reach

    worker = make_worker(
        client=fake_client,
        handlers={TaskType.DETECT_CUT_PLANES: handler},
        cancel_poll_interval_s=0.05,
    )
    await worker.run_one()

    assert fake_client.completed_tasks == []
    assert len(fake_client.failed_tasks) == 1
    assert "cancelled" in fake_client.failed_tasks[0]["error"].lower()


def test_registry_contains_expected_types():
    assert TaskType.DETECT_CUT_PLANES in TASK_PARAMS_SCHEMAS
    assert TaskType.MODEL_INITIALIZING in TASK_PARAMS_SCHEMAS


def test_detect_cut_planes_schema_rejects_extra_fields():
    import pydantic
    with pytest.raises(pydantic.ValidationError):
        DetectCutPlanesParams(input_path="/tmp/x.stl", input_file="nope")


# ----- Worker payload-logger wiring -----------------------------------------


@pytest.mark.asyncio
async def test_worker_constructs_payload_logger_when_shared_volume_set(
    make_worker, fake_client, tmp_path,
):
    worker = make_worker(
        client=fake_client,
        shared_volume_path=str(tmp_path / "shared"),
    )
    assert worker._payload_logger is not None
    assert worker._payload_logger.enabled is True
    assert (tmp_path / "shared" / "_worker_payloads" / "w").is_dir()


@pytest.mark.asyncio
async def test_worker_disabled_when_shared_volume_unset(make_worker, fake_client):
    """Existing tests rely on this — no shared_volume_path means no logger."""
    worker = make_worker(client=fake_client)
    assert worker._payload_logger is not None
    assert worker._payload_logger.enabled is False


@pytest.mark.asyncio
async def test_worker_disabled_via_env_flag(
    make_worker, fake_client, tmp_path, monkeypatch,
):
    monkeypatch.setenv("WORKER_PAYLOAD_LOG_ENABLED", "false")
    worker = make_worker(
        client=fake_client,
        shared_volume_path=str(tmp_path / "shared"),
    )
    assert worker._payload_logger.enabled is False


@pytest.mark.asyncio
async def test_worker_retention_env_falls_back_on_bad_value(
    make_worker, fake_client, tmp_path, monkeypatch, caplog,
):
    monkeypatch.setenv("WORKER_PAYLOAD_LOG_RETENTION_DAYS", "abc")
    with caplog.at_level("WARNING"):
        worker = make_worker(
            client=fake_client,
            shared_volume_path=str(tmp_path / "shared"),
        )
    assert worker._payload_logger.retention_days == 14
    assert any("retention" in r.message.lower() for r in caplog.records)


@pytest.mark.asyncio
async def test_worker_retention_env_falls_back_on_zero(
    make_worker, fake_client, tmp_path, monkeypatch,
):
    monkeypatch.setenv("WORKER_PAYLOAD_LOG_RETENTION_DAYS", "0")
    worker = make_worker(
        client=fake_client,
        shared_volume_path=str(tmp_path / "shared"),
    )
    assert worker._payload_logger.retention_days == 14


@pytest.mark.asyncio
async def test_worker_sanitizes_worker_id_in_log_path(
    make_worker, fake_client, tmp_path,
):
    """worker_id with slashes/.. must not escape into a sibling directory."""
    make_worker(
        client=fake_client,
        worker_id="../etc/passwd",
        shared_volume_path=str(tmp_path / "shared"),
    )
    children = list((tmp_path / "shared" / "_worker_payloads").iterdir())
    assert len(children) == 1
    sanitized = children[0].name
    # Path separators are what would actually escape into a sibling dir;
    # `.` characters are allowed (the dir is a single segment).
    assert "/" not in sanitized
    assert "\\" not in sanitized
    assert "passwd" in sanitized
    # The directory must be a direct child of _worker_payloads.
    assert children[0].parent.name == "_worker_payloads"


# ----- _run_one capture -----------------------------------------------------


@pytest.mark.asyncio
async def test_worker_writes_typed_record_on_happy_path(
    make_worker, fake_client, tmp_path,
):
    (tmp_path / "fake.stl").write_bytes(b"solid\nendsolid\n")
    fake_client.queue_task(
        task_type=TaskType.DETECT_CUT_PLANES,
        params={"input_path": str(tmp_path / "fake.stl"), "max_results": 3},
    )

    async def handler(ctx, params):
        return {"planes": []}

    shared = tmp_path / "shared"
    worker = make_worker(
        client=fake_client,
        handlers={TaskType.DETECT_CUT_PLANES: handler},
        shared_volume_path=str(shared),
    )
    await worker.run_one()
    worker._payload_logger.close()

    payload_dir = shared / "_worker_payloads" / "w"
    files = list(payload_dir.glob("payloads-*.jsonl"))
    assert len(files) == 1
    entry = json.loads(files[0].read_text(encoding="utf-8").strip())
    assert entry["task_type"] == "detect_cut_planes"
    assert entry["params"]["max_results"] == 3


@pytest.mark.asyncio
async def test_worker_writes_typed_record_even_on_schema_rejection(
    make_worker, fake_client, tmp_path,
):
    """Malformed payloads are exactly the bugs we most want to replay."""
    (tmp_path / "fake.stl").write_bytes(b"solid\nendsolid\n")
    fake_client.queue_task(
        task_type=TaskType.DETECT_CUT_PLANES,
        params={"input_path": str(tmp_path / "fake.stl"), "input_file": "oops"},
    )

    async def handler(ctx, params):
        raise AssertionError("must not run")

    shared = tmp_path / "shared"
    worker = make_worker(
        client=fake_client,
        handlers={TaskType.DETECT_CUT_PLANES: handler},
        shared_volume_path=str(shared),
    )
    await worker.run_one()
    worker._payload_logger.close()

    files = list((shared / "_worker_payloads" / "w").glob("payloads-*.jsonl"))
    entry = json.loads(files[0].read_text(encoding="utf-8").strip())
    # Captured BEFORE schema validation, so the bad field is preserved.
    assert entry["params"]["input_file"] == "oops"
    # And the task itself was failed:
    assert len(fake_client.failed_tasks) == 1


# ----- terminal-report failure visibility -----------------------------------
#
# The finally block in _run_one wraps the terminal complete()/fail() call in
# try/except. BackendClient retries transient errors inside _retry, so an
# exception reaching that except means retries were exhausted (backend down
# longer than the retry window) or a non-transient error surfaced. Previously
# the except was a bare `pass`, silently swallowing the failure: a task whose
# handler succeeded and whose outputs uploaded fine, but whose complete()
# call failed, was left in_progress on the backend with zero operator
# visibility. The fix logs at ERROR (naming the task, the terminal method,
# and the outcome) without re-raising — the polling loop must keep running
# other tasks. These tests pin that contract.


class _FlakyCompleteClient(FakeBackendClient):
    """FakeBackendClient whose ``complete`` raises, simulating a terminal
    report that fails after the BackendClient's own retries are exhausted."""

    def __init__(self, exc: Exception) -> None:
        super().__init__()
        self._exc = exc

    async def complete(self, task_id: int, result: dict) -> None:
        raise self._exc


class _FlakyFailClient(FakeBackendClient):
    """FakeBackendClient whose ``fail`` raises, simulating the same on the
    failure path."""

    def __init__(self, exc: Exception) -> None:
        super().__init__()
        self._exc = exc

    async def fail(self, task_id: int, error: str) -> None:
        raise self._exc


@pytest.mark.asyncio
async def test_worker_logs_error_when_complete_report_fails(
    make_worker, tmp_path, caplog,
):
    """A handler that succeeds but whose complete() call fails (backend down
    past the retry window) must not crash the worker, and must be logged at
    ERROR so the operator sees the task was never recorded as complete."""
    (tmp_path / "fake.stl").write_bytes(b"solid\nnendsolid\n")
    flaky = _FlakyCompleteClient(RuntimeError("backend unreachable"))
    flaky.queue_task(
        task_type=TaskType.DETECT_CUT_PLANES,
        params={"input_path": str(tmp_path / "fake.stl")},
    )

    async def handler(ctx, params):
        return {"planes": []}

    with caplog.at_level("ERROR"):
        worker = make_worker(client=flaky, handlers={TaskType.DETECT_CUT_PLANES: handler})
        await worker.run_one()

    # The worker did not crash and did not record the completion.
    assert flaky.completed_tasks == []
    # An ERROR log surfaced the lost terminal report.
    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(error_records) == 1
    msg = error_records[0].getMessage()
    assert "terminal complete report failed" in msg
    assert "backend unreachable" in msg


@pytest.mark.asyncio
async def test_worker_logs_error_when_fail_report_fails(
    make_worker, tmp_path, caplog,
):
    """A handler that raises, but whose fail() call also fails, must not crash
    the worker and must be logged at ERROR — the handler error is otherwise
    lost and the task stays in_progress with no trace."""
    (tmp_path / "fake.stl").write_bytes(b"solid\nnendsolid\n")
    flaky = _FlakyFailClient(RuntimeError("backend unreachable"))
    flaky.queue_task(
        task_type=TaskType.DETECT_CUT_PLANES,
        params={"input_path": str(tmp_path / "fake.stl")},
    )

    async def handler(ctx, params):
        raise RuntimeError("handler boom")

    with caplog.at_level("ERROR"):
        worker = make_worker(client=flaky, handlers={TaskType.DETECT_CUT_PLANES: handler})
        await worker.run_one()

    # The worker did not crash and did not record the failure.
    assert flaky.failed_tasks == []
    # Two ERROR records: one for the handler exception, one for the lost
    # terminal report. The terminal-report one must name the fail method.
    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    terminal_records = [
        r for r in error_records if "terminal fail report failed" in r.getMessage()
    ]
    assert len(terminal_records) == 1
    assert "backend unreachable" in terminal_records[0].getMessage()


@pytest.mark.asyncio
async def test_worker_continues_polling_after_terminal_report_failure(
    make_worker, tmp_path,
):
    """A failed terminal report on one task must not strand subsequent tasks:
    the worker's polling loop must keep running. We verify by running two
    tasks back-to-back where the first's complete() fails but the second's
    backend is healthy."""
    (tmp_path / "fake.stl").write_bytes(b"solid\nnendsolid\n")

    # First client: complete() raises.
    flaky = _FlakyCompleteClient(RuntimeError("transient outage"))
    flaky.queue_task(
        task_type=TaskType.DETECT_CUT_PLANES,
        params={"input_path": str(tmp_path / "fake.stl")},
    )

    async def handler(ctx, params):
        return {"planes": []}

    worker = make_worker(client=flaky, handlers={TaskType.DETECT_CUT_PLANES: handler})
    # First task: handler succeeds, complete() fails — must not raise.
    await worker.run_one()
    assert flaky.completed_tasks == []

    # Swap in a healthy client for the second task and confirm the loop
    # still processes it end-to-end. This exercises the non-raising contract:
    # if the first failure had escaped, run_one() would have raised and this
    # second cycle would never run.
    healthy = FakeBackendClient()
    healthy.queue_task(
        task_type=TaskType.DETECT_CUT_PLANES,
        params={"input_path": str(tmp_path / "fake.stl")},
    )
    worker._client = healthy
    await worker.run_one()
    assert len(healthy.completed_tasks) == 1


# ----- heartbeat-before-prepare_inputs ordering ------------------------------
#
# Remote-mode prepare_inputs downloads GB-scale input files (colmap-splat
# PLYs, Neural-Canvas splats) that can take minutes. The heartbeat must be
# ticking *during* that download or the backend's stale-task sweeper reclaims
# the task while the worker is still fetching it. _run_one used to call
# start_heartbeat() after prepare_inputs(); these tests pin the new order.
#
# The registered DetectCutPlanesParams schema uses extra="forbid" and requires
# input_path, so it can't carry the ``input_files`` dict that triggers the
# remote download path. We swap in a permissive schema for the test task type
# (monkeypatch restores it after).


class _PermissiveParams(TaskParamsBase):
    model_config = ConfigDict(extra="allow")


class _HeartbeatObservingClient(FakeBackendClient):
    """Records how many progress (heartbeat) events had landed by the time
    ``download_file`` ran, so a test can assert the heartbeat was already
    active during input staging."""

    def __init__(self) -> None:
        super().__init__()
        self.progress_count_at_download: int | None = None

    async def download_file(self, task_id, filename, dest):
        # A real BackendClient.download_file suspends on network I/O while
        # streaming bytes — that suspension is what lets the heartbeat task
        # (created just before prepare_inputs) get dispatched by the event
        # loop and tick. The FakeBackendClient writes bytes synchronously
        # with no suspension, so without an explicit yield the heartbeat
        # task never runs before download_file returns. Sleep briefly to
        # model the I/O wait, then snapshot heartbeat activity.
        await asyncio.sleep(0.05)
        self.progress_count_at_download = len(self.progress_events)
        await super().download_file(task_id, filename, dest)


@pytest.mark.asyncio
async def test_heartbeat_starts_before_prepare_inputs(
    make_worker, tmp_path, monkeypatch,
):
    """A remote-mode task (input_files) must have at least one heartbeat
    tick recorded before download_file runs — i.e. start_heartbeat precedes
    prepare_inputs. Without the reorder, progress_events is empty here and
    the sweeper could reclaim the task mid-download."""
    monkeypatch.setitem(
        TASK_PARAMS_SCHEMAS, TaskType.DETECT_CUT_PLANES, _PermissiveParams,
    )
    observer = _HeartbeatObservingClient()
    task = observer.queue_task(
        task_type=TaskType.DETECT_CUT_PLANES,
        params={"input_files": {"mesh": "scene.ply"}, "max_results": 1},
    )
    observer.queue_file(task.id, "scene.ply", b"pretend-PLY")

    async def handler(ctx, params):
        return {"planes": []}

    worker = make_worker(
        client=observer,
        handlers={TaskType.DETECT_CUT_PLANES: handler},
        heartbeat_interval_s=0.01,
    )
    await worker.run_one()

    assert observer.progress_count_at_download is not None
    assert observer.progress_count_at_download >= 1, (
        "heartbeat must be active before prepare_inputs downloads inputs, "
        "else the stale-task sweeper can reclaim the task mid-download"
    )
    assert len(observer.completed_tasks) == 1


@pytest.mark.asyncio
async def test_heartbeat_stopped_after_prepare_inputs_failure(
    make_worker, tmp_path, monkeypatch,
):
    """If prepare_inputs raises (e.g. a missing remote input / 404), the
    heartbeat started ahead of it must still be torn down — the finally
    block calls progress.stop() regardless of where the failure came from.
    A leaked heartbeat task would outlive the task and double-start the next
    one (start_heartbeat rejects a double start)."""
    monkeypatch.setitem(
        TASK_PARAMS_SCHEMAS, TaskType.DETECT_CUT_PLANES, _PermissiveParams,
    )
    observer = _HeartbeatObservingClient()
    observer.queue_task(
        task_type=TaskType.DETECT_CUT_PLANES,
        # input_files set but never staged via queue_file → download 404s.
        params={"input_files": {"mesh": "missing.ply"}, "max_results": 1},
    )

    async def handler(ctx, params):  # pragma: no cover — never reached
        return {"planes": []}

    worker = make_worker(
        client=observer,
        handlers={TaskType.DETECT_CUT_PLANES: handler},
        heartbeat_interval_s=0.01,
    )
    await worker.run_one()

    # The task failed (download raised FileNotFoundError) and was reported.
    assert len(observer.failed_tasks) == 1
    assert observer.progress_count_at_download is not None
    assert observer.progress_count_at_download >= 1, (
        "heartbeat must have started before the failed prepare_inputs"
    )
    # The heartbeat task object is cleared after stop(). A second run_one on
    # the same worker must not blow up on a double start — which it would if
    # stop() left the prior heartbeat task dangling. Run a healthy task next.
    healthy = FakeBackendClient()
    healthy.queue_task(
        task_type=TaskType.DETECT_CUT_PLANES,
        params={"input_files": {"mesh": "ok.ply"}},
    )
    healthy.queue_file(healthy._queue[-1].id, "ok.ply", b"ok")
    worker._client = healthy
    ran = await worker.run_one()
    assert ran is True
    assert len(healthy.completed_tasks) == 1


# ----- cancel-guard-before-prepare_inputs ordering ---------------------------
#
# The CancelGuard must be active *during* prepare_inputs, not just after it.
# Remote-mode prepare_inputs downloads GB-scale inputs (colmap-splat PLYs,
# Neural-Canvas splats) that can take minutes; a user cancel during that
# window must be detected and abort the download — not burn bandwidth to
# completion before discovering the cancel. _run_one used to start the guard
# only after prepare_inputs returned; these tests pin the new order.


class _CancelDuringDownloadClient(FakeBackendClient):
    """FakeBackendClient that marks the task cancelled after the first
    download, so the cancel poll (running concurrently via CancelGuard)
    picks it up and sets the event before the second download starts."""

    def __init__(self) -> None:
        super().__init__()
        self._download_count = 0

    async def download_file(self, task_id, filename, dest):
        # Yield to the event loop so the CancelGuard's poll task can run
        # and observe the cancelled state we set below.
        await asyncio.sleep(0.02)
        await super().download_file(task_id, filename, dest)
        self._download_count += 1
        if self._download_count == 1:
            # Simulate a user cancel arriving after the first file lands.
            self.cancelled_task_ids.add(task_id)
            # Yield long enough for the CancelGuard poll task (running on
            # the same event loop) to wake, call get_cancel_status, and
            # set the ``cancelled`` event before prepare_inputs re-checks
            # it at the top of the download loop.
            await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_cancel_guard_active_during_prepare_inputs(
    make_worker, tmp_path, monkeypatch,
):
    """A user cancel that arrives during prepare_inputs (while downloading
    inputs) must cause the task to fail as 'cancelled by user' — not
    download every remaining file and then run the handler."""
    monkeypatch.setitem(
        TASK_PARAMS_SCHEMAS, TaskType.DETECT_CUT_PLANES, _PermissiveParams,
    )
    client = _CancelDuringDownloadClient()
    task = client.queue_task(
        task_type=TaskType.DETECT_CUT_PLANES,
        params={"input_files": {"a": "a.ply", "b": "b.ply"}, "max_results": 1},
    )
    client.queue_file(task.id, "a.ply", b"pretend-PLY-a")
    client.queue_file(task.id, "b.ply", b"pretend-PLY-b")

    async def handler(ctx, params):  # pragma: no cover — must not run
        raise AssertionError("handler must not run when cancelled during download")

    worker = make_worker(
        client=client,
        handlers={TaskType.DETECT_CUT_PLANES: handler},
        cancel_poll_interval_s=0.01,
        heartbeat_interval_s=0.01,
    )
    await worker.run_one()

    # The task was reported as cancelled, not completed.
    assert client.completed_tasks == []
    assert len(client.failed_tasks) == 1
    assert "cancelled" in client.failed_tasks[0]["error"].lower()
    # The handler never ran (no output produced), and the second file was
    # not downloaded because prepare_inputs aborted between downloads.
    assert client._download_count == 1


@pytest.mark.asyncio
async def test_cancel_guard_before_prepare_inputs_no_false_cancel(
    make_worker, tmp_path, monkeypatch,
):
    """When no cancel is requested, starting the guard before prepare_inputs
    must not cause a spurious TaskCancelled — the task completes normally
    with all inputs downloaded."""
    monkeypatch.setitem(
        TASK_PARAMS_SCHEMAS, TaskType.DETECT_CUT_PLANES, _PermissiveParams,
    )
    client = FakeBackendClient()
    task = client.queue_task(
        task_type=TaskType.DETECT_CUT_PLANES,
        params={"input_files": {"a": "a.ply", "b": "b.ply"}, "max_results": 1},
    )
    client.queue_file(task.id, "a.ply", b"pretend-PLY-a")
    client.queue_file(task.id, "b.ply", b"pretend-PLY-b")

    async def handler(ctx, params):
        return {"planes": []}

    worker = make_worker(
        client=client,
        handlers={TaskType.DETECT_CUT_PLANES: handler},
        cancel_poll_interval_s=0.01,
    )
    await worker.run_one()

    assert len(client.completed_tasks) == 1
    assert client.failed_tasks == []


# ----- cancel-guard-during-upload_outputs ordering ---------------------------
#
# The CancelGuard must stay active *during* upload_outputs, not just through
# the handler. Remote-mode upload_outputs uploads GB-scale outputs (colmap-
# splat PLYs, Neural-Canvas splats) that can take minutes; a user cancel
# during that window must be detected and abort the upload — not burn
# bandwidth streaming every remaining file to a task the user already
# cancelled, then report it complete. _run_one used to call upload_outputs
# *after* the CancelGuard block exited; these tests pin the fix.


class _CancelDuringUploadClient(FakeBackendClient):
    """FakeBackendClient that marks the task cancelled after the first
    output upload, so the cancel poll (running concurrently via CancelGuard)
    picks it up and sets the event before the second upload starts."""

    def __init__(self) -> None:
        super().__init__()
        self._upload_count = 0

    async def upload_file(self, task_id, filename, src):
        await asyncio.sleep(0.02)
        await super().upload_file(task_id, filename, src)
        self._upload_count += 1
        if self._upload_count == 1:
            # Simulate a user cancel arriving after the first output lands.
            self.cancelled_task_ids.add(task_id)
            # Yield long enough for the CancelGuard poll task (running on
            # the same event loop) to wake, call get_cancel_status, and
            # set the ``cancelled`` event before upload_outputs re-checks
            # it at the top of the upload loop.
            await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_cancel_guard_active_during_upload_outputs(
    make_worker, tmp_path, monkeypatch,
):
    """A user cancel that arrives during upload_outputs (while uploading
    outputs) must cause the task to fail as 'cancelled by user' — not
    upload every remaining file and then report the task complete."""
    monkeypatch.setitem(
        TASK_PARAMS_SCHEMAS, TaskType.DETECT_CUT_PLANES, _PermissiveParams,
    )
    client = _CancelDuringUploadClient()
    task = client.queue_task(
        task_type=TaskType.DETECT_CUT_PLANES,
        params={"input_files": {"mesh": "in.ply"}, "max_results": 1},
    )
    client.queue_file(task.id, "in.ply", b"pretend-PLY")

    async def handler(ctx, params):
        # Produce two output files so the cancel lands between uploads.
        out = ctx.files.output_dir
        (out / "a.stl").write_bytes(b"out-a")
        (out / "b.stl").write_bytes(b"out-b")
        return {"output_files": {"a": "a.stl", "b": "b.stl"}}

    worker = make_worker(
        client=client,
        handlers={TaskType.DETECT_CUT_PLANES: handler},
        cancel_poll_interval_s=0.01,
        heartbeat_interval_s=0.01,
    )
    await worker.run_one()

    # The task was reported as cancelled, not completed.
    assert client.completed_tasks == []
    assert len(client.failed_tasks) == 1
    assert "cancelled" in client.failed_tasks[0]["error"].lower()
    # The first output uploaded, but the second was aborted — the cancel
    # was detected between uploads, not after streaming everything.
    assert client._upload_count == 1


@pytest.mark.asyncio
async def test_cancel_guard_during_upload_no_false_cancel(
    make_worker, tmp_path, monkeypatch,
):
    """When no cancel is requested, keeping the guard active through
    upload_outputs must not cause a spurious TaskCancelled — all outputs
    upload and the task completes normally."""
    monkeypatch.setitem(
        TASK_PARAMS_SCHEMAS, TaskType.DETECT_CUT_PLANES, _PermissiveParams,
    )
    client = FakeBackendClient()
    task = client.queue_task(
        task_type=TaskType.DETECT_CUT_PLANES,
        params={"input_files": {"mesh": "in.ply"}, "max_results": 1},
    )
    client.queue_file(task.id, "in.ply", b"pretend-PLY")

    async def handler(ctx, params):
        out = ctx.files.output_dir
        (out / "a.stl").write_bytes(b"out-a")
        (out / "b.stl").write_bytes(b"out-b")
        return {"output_files": {"a": "a.stl", "b": "b.stl"}}

    worker = make_worker(
        client=client,
        handlers={TaskType.DETECT_CUT_PLANES: handler},
        cancel_poll_interval_s=0.01,
    )
    await worker.run_one()

    assert len(client.completed_tasks) == 1
    assert client.failed_tasks == []
    # Both outputs delivered.
    assert (task.id, "a.stl") in client.uploaded_files
    assert (task.id, "b.stl") in client.uploaded_files

