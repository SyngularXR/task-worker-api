"""Tests for ``prepare_inputs`` / ``upload_outputs`` file transfer.

Focuses on the partial-failure cleanup contract: when publishing outputs
fails partway through (the Nth upload/copy raises after files 1..N-1
succeeded), the partial artifacts must be cleaned up so a retried task
starts clean and no orphaned outputs linger. Mirrors the
``BackendClient.download_file`` partial-file cleanup contract.

Uses ``FakeBackendClient`` (in-memory) for the remote-mode path and real
tmp dirs for the local-mode staging-dir path — no live HTTP needed.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from task_worker_api.context import ClaimedTask, FileContext
from task_worker_api.enums import TaskStatus, TaskType
from task_worker_api.files import prepare_inputs, upload_outputs
from task_worker_api.testing import FakeBackendClient


# ---------------------------------------------------------------------------#
# Helpers
# ---------------------------------------------------------------------------#

def _claimed(task_id: int, *, params: dict) -> ClaimedTask:
    return ClaimedTask(
        id=task_id,
        task_type=TaskType.DETECT_CUT_PLANES,
        case_id=None,
        item_key="",
        status=TaskStatus.PENDING,
        params=params,
        worker_id=None,
    )


def _file_ctx(out_dir: Path) -> FileContext:
    """Build a FileContext whose output_dir is ``out_dir`` (input fields
    unused by upload_outputs)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    return FileContext(
        input_dir=out_dir.parent / "in",
        output_dir=out_dir,
        primary_path=out_dir,
        all_paths={},
    )


class _FlakyUploadClient(FakeBackendClient):
    """FakeBackendClient whose ``upload_file`` fails on a target filename.

    Simulates a transient-or-not upload failure partway through a
    multi-file publish: every file whose filename is in ``fail_on`` raises,
    others succeed. Used to exercise the partial-cleanup path.
    """

    def __init__(self, fail_on: set[str]) -> None:
        super().__init__()
        self._fail_on = fail_on

    async def upload_file(self, task_id: int, filename: str, src: Path) -> None:
        if filename in self._fail_on:
            raise RuntimeError(f"upload failed for {filename}")
        await super().upload_file(task_id, filename, src)


# ---------------------------------------------------------------------------#
# upload_outputs — local mode (shared-volume staging dir) partial cleanup
# ---------------------------------------------------------------------------#


@pytest.mark.asyncio
async def test_local_mode_cleans_staging_dir_on_partial_copy_failure(tmp_path):
    """When a copy into the staging dir fails partway through, the
    partially-populated staging dir must be removed so a retried task
    starts clean — the backend's sweeper only removes staging dirs for
    tasks it recorded as complete, so a failed task's partial dir would
    otherwise linger as an orphan.
    """
    shared = tmp_path / "shared"
    out_dir = tmp_path / "work" / "out"
    out_dir.mkdir(parents=True)

    # Two output files; the second's source is missing → copy2 raises.
    (out_dir / "a.stl").write_bytes(b"aaa")
    # "b.stl" deliberately not created → shutil.copy2 raises FileNotFoundError.

    task = _claimed(11, params={"input_path": "/ignored"})
    file_ctx = _file_ctx(out_dir)

    with pytest.raises(FileNotFoundError):
        await upload_outputs(
            task, FakeBackendClient(), file_ctx,
            output_files={"a": "a.stl", "b": "b.stl"},
            shared_volume_path=str(shared),
        )

    staging = shared / "temp" / "11"
    assert not staging.exists(), (
        "partial staging dir must be removed on copy failure"
    )


@pytest.mark.asyncio
async def test_local_mode_cleans_staging_dir_when_second_copy_fails(tmp_path):
    """A more targeted partial-failure: the first copy succeeds (file lands
    in the staging dir), then the second copy fails. The cleanup must
    remove the *entire* staging dir including the successfully-copied
    first file — no partial artifact survives."""
    shared = tmp_path / "shared"
    out_dir = tmp_path / "work" / "out"
    out_dir.mkdir(parents=True)
    (out_dir / "a.stl").write_bytes(b"aaa")
    # "b.stl" missing → second copy fails.

    task = _claimed(14, params={"input_path": "/ignored"})
    file_ctx = _file_ctx(out_dir)

    with pytest.raises(FileNotFoundError):
        await upload_outputs(
            task, FakeBackendClient(), file_ctx,
            output_files={"a": "a.stl", "b": "b.stl"},
            shared_volume_path=str(shared),
        )

    staging = shared / "temp" / "14"
    assert not staging.exists()


@pytest.mark.asyncio
async def test_local_mode_keeps_staging_dir_on_success(tmp_path):
    """The cleanup must not interfere with a successful publish: the
    staging dir and all outputs survive a clean upload_outputs call."""
    shared = tmp_path / "shared"
    out_dir = tmp_path / "work" / "out"
    out_dir.mkdir(parents=True)
    (out_dir / "a.stl").write_bytes(b"aaa")
    (out_dir / "b.stl").write_bytes(b"bbb")

    task = _claimed(12, params={"input_path": "/ignored"})
    file_ctx = _file_ctx(out_dir)

    manifest = await upload_outputs(
        task, FakeBackendClient(), file_ctx,
        output_files={"a": "a.stl", "b": "b.stl"},
        shared_volume_path=str(shared),
    )

    staging = shared / "temp" / "12"
    assert staging.is_dir()
    assert (staging / "a.stl").read_bytes() == b"aaa"
    assert (staging / "b.stl").read_bytes() == b"bbb"
    assert set(manifest.keys()) == {"a", "b"}


@pytest.mark.asyncio
async def test_local_mode_no_staging_dir_when_shared_volume_unset(tmp_path):
    """Without shared_volume_path, upload_outputs returns output_dir paths
    and creates no staging dir — nothing to clean up."""
    out_dir = tmp_path / "work" / "out"
    out_dir.mkdir(parents=True)
    (out_dir / "a.stl").write_bytes(b"aaa")

    task = _claimed(13, params={"input_path": "/ignored"})
    file_ctx = _file_ctx(out_dir)

    manifest = await upload_outputs(
        task, FakeBackendClient(), file_ctx,
        output_files={"a": "a.stl"},
        shared_volume_path=None,
    )

    assert manifest == {"a": str(out_dir / "a.stl")}
    # No staging dir created anywhere.
    assert not (tmp_path / "temp").exists()


# ---------------------------------------------------------------------------#
# upload_outputs — remote mode partial-cleanup visibility
# ---------------------------------------------------------------------------#


@pytest.mark.asyncio
async def test_remote_mode_logs_partial_uploads_on_failure(tmp_path, caplog):
    """When an upload fails after some files uploaded successfully, the
    partial uploads that remain on the backend must be surfaced in a
    WARNING so an operator can reconcile. The exception still propagates
    so the task is marked failed and retried."""
    out_dir = tmp_path / "work" / "out"
    out_dir.mkdir(parents=True)
    (out_dir / "a.stl").write_bytes(b"aaa")
    (out_dir / "b.stl").write_bytes(b"bbb")
    (out_dir / "c.stl").write_bytes(b"ccc")

    task = _claimed(21, params={"input_files": {"mesh": "in.ply"}})
    file_ctx = _file_ctx(out_dir)
    client = _FlakyUploadClient(fail_on={"c.stl"})

    with caplog.at_level("WARNING"):
        with pytest.raises(RuntimeError, match="upload failed for c.stl"):
            await upload_outputs(
                task, client, file_ctx,
                output_files={"a": "a.stl", "b": "b.stl", "c": "c.stl"},
                shared_volume_path=None,
            )

    # a and b uploaded before c failed.
    assert (21, "a.stl") in client.uploaded_files
    assert (21, "b.stl") in client.uploaded_files
    assert (21, "c.stl") not in client.uploaded_files
    # The partial state was surfaced.
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any(
        "2 output file(s)" in r.getMessage()
        and "a.stl" in r.getMessage()
        and "b.stl" in r.getMessage()
        for r in warnings
    )


@pytest.mark.asyncio
async def test_remote_mode_no_warning_when_first_upload_fails(tmp_path, caplog):
    """If the very first upload fails, there are no partial uploads to
    report — the warning must not fire (no orphaned artifacts)."""
    out_dir = tmp_path / "work" / "out"
    out_dir.mkdir(parents=True)
    (out_dir / "a.stl").write_bytes(b"aaa")

    task = _claimed(22, params={"input_files": {"mesh": "in.ply"}})
    file_ctx = _file_ctx(out_dir)
    client = _FlakyUploadClient(fail_on={"a.stl"})

    with caplog.at_level("WARNING"):
        with pytest.raises(RuntimeError):
            await upload_outputs(
                task, client, file_ctx,
                output_files={"a": "a.stl"},
                shared_volume_path=None,
            )

    partial_warnings = [
        r for r in caplog.records
        if r.levelname == "WARNING" and "partial uploads remain" in r.getMessage()
    ]
    assert partial_warnings == []


@pytest.mark.asyncio
async def test_remote_mode_succeeds_without_warning(tmp_path, caplog):
    """A clean multi-file remote publish must not log any partial-upload
    warning."""
    out_dir = tmp_path / "work" / "out"
    out_dir.mkdir(parents=True)
    (out_dir / "a.stl").write_bytes(b"aaa")
    (out_dir / "b.stl").write_bytes(b"bbb")

    task = _claimed(23, params={"input_files": {"mesh": "in.ply"}})
    file_ctx = _file_ctx(out_dir)
    client = FakeBackendClient()

    with caplog.at_level("WARNING"):
        manifest = await upload_outputs(
            task, client, file_ctx,
            output_files={"a": "a.stl", "b": "b.stl"},
            shared_volume_path=None,
        )

    assert set(manifest.keys()) == {"a", "b"}
    assert (23, "a.stl") in client.uploaded_files
    assert (23, "b.stl") in client.uploaded_files
    partial_warnings = [
        r for r in caplog.records
        if r.levelname == "WARNING" and "partial uploads remain" in r.getMessage()
    ]
    assert partial_warnings == []


# ---------------------------------------------------------------------------#
# prepare_inputs — regression: remote-mode downloads every declared input.
# (task_dir cleanup on failure is owned by Worker._run_one's finally; here
# we just pin the prepare_inputs happy path so the upload tests have a
# known-good baseline.)
# ---------------------------------------------------------------------------#


@pytest.mark.asyncio
async def test_prepare_inputs_remote_mode_downloads_all_inputs(tmp_path):
    """Remote-mode prepare_inputs downloads every declared input file into
    in/ and surfaces them in the FileContext."""
    work_dir = tmp_path / "work"
    client = FakeBackendClient()
    client.queue_file(31, "mesh.ply", b"mesh-bytes")
    client.queue_file(31, "meta.json", b"meta-bytes")

    task = _claimed(
        31, params={"input_files": {"mesh": "mesh.ply", "meta": "meta.json"}},
    )
    file_ctx = await prepare_inputs(task, client, work_dir)

    assert (work_dir / "in" / "mesh.ply").read_bytes() == b"mesh-bytes"
    assert (work_dir / "in" / "meta.json").read_bytes() == b"meta-bytes"
    assert file_ctx.all_paths["mesh"].name == "mesh.ply"
    assert file_ctx.all_paths["meta"].name == "meta.json"


# ---------------------------------------------------------------------------#
# prepare_inputs — cancel-during-download: a set ``cancelled`` event must
# abort between batch downloads so a user cancel doesn't wait for every
# remaining GB-scale file to finish streaming.
# ---------------------------------------------------------------------------#


@pytest.mark.asyncio
async def test_prepare_inputs_aborts_when_cancelled_between_downloads(tmp_path):
    """When ``cancelled`` is set, prepare_inputs must raise TaskCancelled
    before downloading the next file in the batch — not download all
    remaining inputs to completion."""
    import asyncio
    from task_worker_api.errors import TaskCancelled

    work_dir = tmp_path / "work"
    client = FakeBackendClient()
    client.queue_file(42, "a.ply", b"aaa")
    client.queue_file(42, "b.ply", b"bbb")
    client.queue_file(42, "c.ply", b"ccc")

    task = _claimed(42, params={"input_files": {"a": "a.ply", "b": "b.ply", "c": "c.ply"}})

    cancelled = asyncio.Event()
    cancelled.set()

    with pytest.raises(TaskCancelled, match="cancelled by user"):
        await prepare_inputs(task, client, work_dir, cancelled=cancelled)

    # No file was downloaded — the event was set before the loop body ran.
    assert not (work_dir / "in" / "a.ply").exists()
    assert not (work_dir / "in" / "b.ply").exists()
    assert not (work_dir / "in" / "c.ply").exists()


@pytest.mark.asyncio
async def test_prepare_inputs_aborts_mid_batch_when_cancel_set_after_first(tmp_path):
    """A cancel detected after the first download must abort before the
    second: the first file is on disk, but the remaining ones are not."""
    import asyncio
    from task_worker_api.errors import TaskCancelled

    work_dir = tmp_path / "work"

    class _CancelAfterFirstClient(FakeBackendClient):
        """Sets the cancel event right after the first download completes,
        simulating a user cancel detected between batch downloads."""
        def __init__(self, event: asyncio.Event) -> None:
            super().__init__()
            self._event = event
            self._download_count = 0

        async def download_file(self, task_id, filename, dest, *, cancelled=None):
            await super().download_file(
                task_id, filename, dest, cancelled=cancelled,
            )
            self._download_count += 1
            if self._download_count == 1:
                self._event.set()

    client = _CancelAfterFirstClient(asyncio.Event())
    client.queue_file(99, "a.ply", b"aaa")
    client.queue_file(99, "b.ply", b"bbb")

    task = _claimed(99, params={"input_files": {"a": "a.ply", "b": "b.ply"}})
    cancelled = asyncio.Event()
    client._event = cancelled

    with pytest.raises(TaskCancelled):
        await prepare_inputs(task, client, work_dir, cancelled=cancelled)

    # First file downloaded, second aborted.
    assert (work_dir / "in" / "a.ply").exists()
    assert not (work_dir / "in" / "b.ply").exists()


@pytest.mark.asyncio
async def test_prepare_inputs_aborts_mid_file_on_single_input(tmp_path):
    """A single-file input set has no between-files boundary, so the only
    way a cancel can land during the download is if the event reaches
    ``download_file``. Before that threading, a lone GB-scale input
    (colmap-splat PLY, Neural-Canvas splat) streamed to completion after
    the user cancelled and prepare_inputs returned a FileContext as if
    nothing had happened."""
    import asyncio
    from task_worker_api.errors import TaskCancelled

    work_dir = tmp_path / "work"

    class _CancelDuringSoloDownload(FakeBackendClient):
        """Sets the cancel event *during* the download — what a CancelGuard
        poll firing mid-stream looks like — then delegates to the fake's
        download_file, which honours the event as the real client does."""
        def __init__(self, event: asyncio.Event) -> None:
            super().__init__()
            self._event = event

        async def download_file(self, task_id, filename, dest, *, cancelled=None):
            self._event.set()
            await super().download_file(
                task_id, filename, dest, cancelled=cancelled,
            )

    cancelled = asyncio.Event()
    client = _CancelDuringSoloDownload(cancelled)
    client.queue_file(42, "solo.ply", b"pretend-multi-GB-PLY")

    task = _claimed(42, params={"input_files": {"mesh": "solo.ply"}})

    with pytest.raises(TaskCancelled):
        await prepare_inputs(task, client, work_dir, cancelled=cancelled)

    assert not (work_dir / "in" / "solo.ply").exists()


@pytest.mark.asyncio
async def test_prepare_inputs_passes_cancel_event_to_download_file(tmp_path):
    """The guard's own event object must be handed to ``download_file`` —
    a copy or a fresh event would never see the guard set the original."""
    import asyncio

    class _RecordingClient(FakeBackendClient):
        def __init__(self) -> None:
            super().__init__()
            self.seen: list = []

        async def download_file(self, task_id, filename, dest, *, cancelled=None):
            self.seen.append(cancelled)
            await super().download_file(
                task_id, filename, dest, cancelled=cancelled,
            )

    client = _RecordingClient()
    client.queue_file(3, "a.ply", b"aa")
    task = _claimed(3, params={"input_files": {"mesh": "a.ply"}})
    cancelled = asyncio.Event()

    await prepare_inputs(task, client, tmp_path / "work", cancelled=cancelled)

    assert client.seen == [cancelled]
    assert client.seen[0] is cancelled


@pytest.mark.asyncio
async def test_prepare_inputs_accepts_legacy_download_file_signature(tmp_path):
    """A client written against the pre-``cancelled`` signature must keep
    working, cancel guard and all.

    Worker repos pass their own clients and test doubles to
    ``Worker(client=...)``, and the worker always has a CancelGuard running
    over ``prepare_inputs`` — so sending ``cancelled=`` unconditionally would
    TypeError on every consumer that hasn't updated its override yet."""
    import asyncio

    class _LegacyClient(FakeBackendClient):
        """Three positional args, no ``cancelled`` — the SDK's own signature
        before this feature, and what sibling repos still implement."""
        async def download_file(self, task_id, filename, dest):
            await FakeBackendClient.download_file(self, task_id, filename, dest)

    client = _LegacyClient()
    client.queue_file(8, "a.ply", b"aa")
    task = _claimed(8, params={"input_files": {"mesh": "a.ply"}})

    ctx = await prepare_inputs(
        task, client, tmp_path / "work", cancelled=asyncio.Event(),
    )

    assert ctx.primary_path.read_bytes() == b"aa"


@pytest.mark.asyncio
async def test_prepare_inputs_legacy_client_still_aborts_between_files(tmp_path):
    """The compatibility path must not cost a legacy client the cancellation
    it already had: ``prepare_inputs``' own between-files check still fires,
    it just can't abort mid-stream."""
    import asyncio
    from task_worker_api.errors import TaskCancelled

    work_dir = tmp_path / "work"

    class _LegacyCancelAfterFirst(FakeBackendClient):
        def __init__(self, event: asyncio.Event) -> None:
            super().__init__()
            self._event = event

        async def download_file(self, task_id, filename, dest):
            await FakeBackendClient.download_file(self, task_id, filename, dest)
            self._event.set()

    cancelled = asyncio.Event()
    client = _LegacyCancelAfterFirst(cancelled)
    client.queue_file(9, "a.ply", b"aa")
    client.queue_file(9, "b.ply", b"bb")
    task = _claimed(9, params={"input_files": {"a": "a.ply", "b": "b.ply"}})

    with pytest.raises(TaskCancelled):
        await prepare_inputs(task, client, work_dir, cancelled=cancelled)

    assert (work_dir / "in" / "a.ply").exists()
    assert not (work_dir / "in" / "b.ply").exists()


@pytest.mark.asyncio
async def test_prepare_inputs_passes_cancel_to_kwargs_only_override(tmp_path):
    """A ``**kwargs`` passthrough override (a common test-double shape) must
    still receive the event — it can forward it to the real client."""
    import asyncio

    class _KwargsClient(FakeBackendClient):
        def __init__(self) -> None:
            super().__init__()
            self.seen: list = []

        async def download_file(self, *args, **kwargs):
            self.seen.append(kwargs.get("cancelled"))
            await FakeBackendClient.download_file(self, *args, **kwargs)

    client = _KwargsClient()
    client.queue_file(10, "a.ply", b"aa")
    task = _claimed(10, params={"input_files": {"mesh": "a.ply"}})
    cancelled = asyncio.Event()

    await prepare_inputs(task, client, tmp_path / "work", cancelled=cancelled)

    assert client.seen == [cancelled]


@pytest.mark.asyncio
async def test_prepare_inputs_no_cancel_event_downloads_all(tmp_path):
    """When ``cancelled`` is None (the default), prepare_inputs behaves
    exactly as before — all files download regardless of cancel state.
    This pins backward compatibility for callers that don't pass the event."""
    work_dir = tmp_path / "work"
    client = FakeBackendClient()
    client.queue_file(7, "x.ply", b"xx")
    client.queue_file(7, "y.ply", b"yy")

    task = _claimed(7, params={"input_files": {"x": "x.ply", "y": "y.ply"}})
    file_ctx = await prepare_inputs(task, client, work_dir)

    assert (work_dir / "in" / "x.ply").exists()
    assert (work_dir / "in" / "y.ply").exists()
    assert set(file_ctx.all_paths.keys()) == {"x", "y"}


# ---------------------------------------------------------------------------#
# upload_outputs — cancel-during-upload: a set ``cancelled`` event must
# abort between batch uploads so a user cancel doesn't wait for every
# remaining GB-scale output file to finish streaming. Mirrors the
# cancel-during-download guard in prepare_inputs.
# ---------------------------------------------------------------------------#


@pytest.mark.asyncio
async def test_upload_outputs_remote_aborts_when_cancelled_before_first(tmp_path):
    """When ``cancelled`` is already set, remote-mode upload_outputs must
    raise TaskCancelled before uploading any file — not stream the first
    file to a task the user already cancelled."""
    import asyncio
    from task_worker_api.errors import TaskCancelled

    out_dir = tmp_path / "work" / "out"
    out_dir.mkdir(parents=True)
    (out_dir / "a.stl").write_bytes(b"aaa")
    (out_dir / "b.stl").write_bytes(b"bbb")

    task = _claimed(51, params={"input_files": {"mesh": "in.ply"}})
    file_ctx = _file_ctx(out_dir)
    client = FakeBackendClient()

    cancelled = asyncio.Event()
    cancelled.set()

    with pytest.raises(TaskCancelled, match="cancelled by user"):
        await upload_outputs(
            task, client, file_ctx,
            output_files={"a": "a.stl", "b": "b.stl"},
            shared_volume_path=None,
            cancelled=cancelled,
        )

    # No file was uploaded — the event was set before the loop body ran.
    assert (51, "a.stl") not in client.uploaded_files
    assert (51, "b.stl") not in client.uploaded_files


@pytest.mark.asyncio
async def test_upload_outputs_remote_aborts_mid_batch_when_cancel_set_after_first(tmp_path):
    """A cancel detected after the first upload must abort before the
    second: the first file is on the backend, but the remaining ones are
    not — not streamed to a cancelled task."""
    import asyncio
    from task_worker_api.errors import TaskCancelled

    out_dir = tmp_path / "work" / "out"
    out_dir.mkdir(parents=True)
    (out_dir / "a.stl").write_bytes(b"aaa")
    (out_dir / "b.stl").write_bytes(b"bbb")
    (out_dir / "c.stl").write_bytes(b"ccc")

    task = _claimed(52, params={"input_files": {"mesh": "in.ply"}})
    file_ctx = _file_ctx(out_dir)

    class _CancelAfterFirstUpload(FakeBackendClient):
        """Sets the cancel event right after the first upload completes,
        simulating a user cancel detected between batch uploads."""
        def __init__(self, event: asyncio.Event) -> None:
            super().__init__()
            self._event = event
            self._upload_count = 0

        async def upload_file(self, task_id, filename, src):
            await super().upload_file(task_id, filename, src)
            self._upload_count += 1
            if self._upload_count == 1:
                self._event.set()

    cancelled = asyncio.Event()
    client = _CancelAfterFirstUpload(cancelled)

    with pytest.raises(TaskCancelled):
        await upload_outputs(
            task, client, file_ctx,
            output_files={"a": "a.stl", "b": "b.stl", "c": "c.stl"},
            shared_volume_path=None,
            cancelled=cancelled,
        )

    # First file uploaded, second and third aborted.
    assert (52, "a.stl") in client.uploaded_files
    assert (52, "b.stl") not in client.uploaded_files
    assert (52, "c.stl") not in client.uploaded_files


@pytest.mark.asyncio
async def test_upload_outputs_local_aborts_when_cancelled(tmp_path):
    """Local-mode (shared-volume staging) upload_outputs must also honor a
    set ``cancelled`` event — abort between copies and clean up the partial
    staging dir, rather than copying every remaining file."""
    import asyncio
    from task_worker_api.errors import TaskCancelled

    shared = tmp_path / "shared"
    out_dir = tmp_path / "work" / "out"
    out_dir.mkdir(parents=True)
    (out_dir / "a.stl").write_bytes(b"aaa")
    (out_dir / "b.stl").write_bytes(b"bbb")

    task = _claimed(53, params={"input_path": "/ignored"})
    file_ctx = _file_ctx(out_dir)

    cancelled = asyncio.Event()
    cancelled.set()

    with pytest.raises(TaskCancelled, match="cancelled by user"):
        await upload_outputs(
            task, FakeBackendClient(), file_ctx,
            output_files={"a": "a.stl", "b": "b.stl"},
            shared_volume_path=str(shared),
            cancelled=cancelled,
        )

    # Cancel raised before the first copy → no staging dir created.
    staging = shared / "temp" / "53"
    assert not staging.exists()


@pytest.mark.asyncio
async def test_upload_outputs_no_cancel_event_uploads_all(tmp_path):
    """When ``cancelled`` is None (the default), upload_outputs behaves
    exactly as before — all files upload regardless of cancel state.
    This pins backward compatibility for callers that don't pass the event."""
    out_dir = tmp_path / "work" / "out"
    out_dir.mkdir(parents=True)
    (out_dir / "a.stl").write_bytes(b"aaa")
    (out_dir / "b.stl").write_bytes(b"bbb")

    task = _claimed(54, params={"input_files": {"mesh": "in.ply"}})
    file_ctx = _file_ctx(out_dir)
    client = FakeBackendClient()

    manifest = await upload_outputs(
        task, client, file_ctx,
        output_files={"a": "a.stl", "b": "b.stl"},
        shared_volume_path=None,
    )

    assert set(manifest.keys()) == {"a", "b"}
    assert (54, "a.stl") in client.uploaded_files
    assert (54, "b.stl") in client.uploaded_files


# ---------------------------------------------------------------------------#
# Local-mode copies are chunked and cancellable *mid-file*.
#
# A multi-GB local copy (Blender-CLI .blend inputs, Neural-Canvas splats on a
# network-mounted shared volume) used to run as one blocking ``shutil.copy2``:
# the event loop froze for the whole copy, so the heartbeat stopped ticking
# (the backend's stale-task sweeper could reclaim a task still copying in) and
# the CancelGuard froze with it (a user cancel stayed invisible until the copy
# finished). The copy now yields between chunks and re-checks ``cancelled``.
#
# Each test shrinks ``_COPY_CHUNK_BYTES`` so a small file spans many chunks,
# then sets the event after the copy has reached its first per-chunk yield —
# a cancel arriving *during* the copy, not before it.
# ---------------------------------------------------------------------------#


@pytest.mark.asyncio
async def test_prepare_inputs_local_aborts_on_mid_copy_cancel(
    tmp_path, monkeypatch,
):
    """A cancel that lands while a local-mode input copy is in flight must
    raise TaskCancelled and leave no partial file behind — not run the copy
    to completion first."""
    import asyncio
    from task_worker_api import files as files_mod
    from task_worker_api.errors import TaskCancelled

    monkeypatch.setattr(files_mod, "_COPY_CHUNK_BYTES", 4)

    src = tmp_path / "big.blend"
    src.write_bytes(b"x" * 4000)  # 1000 chunks
    work_dir = tmp_path / "work"

    task = _claimed(61, params={"input_path": str(src)})
    cancelled = asyncio.Event()

    copying = asyncio.create_task(
        prepare_inputs(task, FakeBackendClient(), work_dir, cancelled=cancelled)
    )
    # Hand the loop to the copy; it suspends at its first per-chunk yield.
    # (Under the old blocking copy2 this instead ran the copy to completion,
    # and the cancel below arrived too late to be seen at all.)
    await asyncio.sleep(0)
    cancelled.set()

    with pytest.raises(TaskCancelled, match="cancelled by user"):
        await copying

    assert not (work_dir / "in" / "big.blend").exists(), (
        "partial input copy must be removed when a cancel aborts it mid-file"
    )


@pytest.mark.asyncio
async def test_upload_outputs_local_aborts_on_mid_copy_cancel(
    tmp_path, monkeypatch,
):
    """Same for the local-mode staging copy in upload_outputs: a cancel
    mid-copy raises TaskCancelled, and the existing partial-cleanup contract
    still removes the whole staging dir."""
    import asyncio
    from task_worker_api import files as files_mod
    from task_worker_api.errors import TaskCancelled

    monkeypatch.setattr(files_mod, "_COPY_CHUNK_BYTES", 4)

    shared = tmp_path / "shared"
    out_dir = tmp_path / "work" / "out"
    out_dir.mkdir(parents=True)
    (out_dir / "big.ply").write_bytes(b"y" * 4000)  # 1000 chunks

    task = _claimed(62, params={"input_path": "/ignored"})
    file_ctx = _file_ctx(out_dir)
    cancelled = asyncio.Event()

    uploading = asyncio.create_task(
        upload_outputs(
            task, FakeBackendClient(), file_ctx,
            output_files={"big": "big.ply"},
            shared_volume_path=str(shared),
            cancelled=cancelled,
        )
    )
    await asyncio.sleep(0)
    cancelled.set()

    with pytest.raises(TaskCancelled, match="cancelled by user"):
        await uploading

    assert not (shared / "temp" / "62").exists(), (
        "staging dir must be cleaned up when a cancel aborts a copy mid-file"
    )


@pytest.mark.asyncio
async def test_local_multi_chunk_copies_are_faithful_without_cancel_event(
    tmp_path, monkeypatch,
):
    """Regression: with ``cancelled=None`` (the default) a multi-chunk copy
    must still be a faithful ``copy2`` — every byte in order, and metadata
    preserved via ``copystat`` — for both local-mode call sites."""
    import os
    from task_worker_api import files as files_mod

    monkeypatch.setattr(files_mod, "_COPY_CHUNK_BYTES", 4)

    # Non-uniform payload whose length isn't a chunk multiple, so a dropped
    # or reordered chunk (or a lost tail) shows up as a byte mismatch.
    payload = bytes(range(256)) * 5 + b"tail"
    src = tmp_path / "in.blend"
    src.write_bytes(payload)
    os.utime(src, (1_000_000, 1_000_000))

    work_dir = tmp_path / "work"
    task = _claimed(63, params={"input_path": str(src)})

    file_ctx = await prepare_inputs(task, FakeBackendClient(), work_dir)

    dest = work_dir / "in" / "in.blend"
    assert file_ctx.primary_path == dest
    assert dest.read_bytes() == payload
    assert dest.stat().st_mtime == src.stat().st_mtime, (
        "copystat must run so the chunked copy keeps copy2's metadata semantics"
    )

    # Same for the staging copy on the way out.
    (file_ctx.output_dir / "out.ply").write_bytes(payload)
    shared = tmp_path / "shared"
    manifest = await upload_outputs(
        task, FakeBackendClient(), file_ctx,
        output_files={"out": "out.ply"},
        shared_volume_path=str(shared),
    )

    staged = shared / "temp" / "63" / "out.ply"
    assert manifest == {"out": str(staged)}
    assert staged.read_bytes() == payload


@pytest.mark.asyncio
async def test_copyfile_async_runs_filesystem_operations_off_loop(
    tmp_path, monkeypatch,
):
    """Slow filesystem calls must run on a worker thread, not the loop."""
    import builtins
    import shutil
    import threading
    from task_worker_api import files as files_mod

    src = tmp_path / "source.bin"
    dest = tmp_path / "dest.bin"
    src.write_bytes(b"payload")

    loop_thread = threading.get_ident()
    calls = []
    real_open = builtins.open
    real_copystat = shutil.copystat

    class RecordingFile:
        def __init__(self, wrapped):
            self._wrapped = wrapped

        def read(self, *args):
            calls.append(("read", threading.get_ident()))
            return self._wrapped.read(*args)

        def write(self, *args):
            calls.append(("write", threading.get_ident()))
            return self._wrapped.write(*args)

        def close(self):
            calls.append(("close", threading.get_ident()))
            return self._wrapped.close()

    def recording_open(path, mode):
        calls.append(("open", threading.get_ident()))
        return RecordingFile(real_open(path, mode))

    def recording_copystat(source, destination):
        calls.append(("copystat", threading.get_ident()))
        return real_copystat(source, destination)

    monkeypatch.setattr(builtins, "open", recording_open)
    monkeypatch.setattr(shutil, "copystat", recording_copystat)

    await files_mod._copyfile_async(src, dest)

    assert {name for name, _ in calls} == {
        "open", "read", "write", "close", "copystat",
    }
    assert all(thread_id != loop_thread for _, thread_id in calls)
    assert dest.read_bytes() == b"payload"


@pytest.mark.asyncio
async def test_copyfile_async_same_file_preserves_source(tmp_path):
    """Copying a path onto itself raises without truncating its contents."""
    import shutil
    from task_worker_api import files as files_mod

    src = tmp_path / "same.bin"
    src.write_bytes(b"keep me")

    with pytest.raises(shutil.SameFileError):
        await files_mod._copyfile_async(src, src)

    assert src.read_bytes() == b"keep me"


@pytest.mark.asyncio
async def test_copyfile_async_missing_source_preserves_destination(tmp_path):
    """A failure before opening dest must not delete an existing file."""
    from task_worker_api import files as files_mod

    dest = tmp_path / "existing.bin"
    dest.write_bytes(b"keep me")

    with pytest.raises(FileNotFoundError):
        await files_mod._copyfile_async(tmp_path / "missing.bin", dest)

    assert dest.read_bytes() == b"keep me"


@pytest.mark.asyncio
async def test_copyfile_async_dest_open_failure_preserves_destination(
    tmp_path, monkeypatch,
):
    """A failed destination open must not trigger partial-copy cleanup."""
    import builtins
    from task_worker_api import files as files_mod

    src = tmp_path / "source.bin"
    dest = tmp_path / "existing.bin"
    src.write_bytes(b"source")
    dest.write_bytes(b"keep me")
    real_open = builtins.open

    def fail_dest_open(path, mode):
        if Path(path) == dest and mode == "wb":
            raise OSError("destination open failed")
        return real_open(path, mode)

    monkeypatch.setattr(builtins, "open", fail_dest_open)

    with pytest.raises(OSError, match="destination open failed"):
        await files_mod._copyfile_async(src, dest)

    assert dest.read_bytes() == b"keep me"


@pytest.mark.asyncio
async def test_copyfile_async_copystat_failure_removes_copy(
    tmp_path, monkeypatch,
):
    """Metadata failure is still a failed copy and leaves no artifact."""
    import shutil
    from task_worker_api import files as files_mod

    src = tmp_path / "source.bin"
    dest = tmp_path / "dest.bin"
    src.write_bytes(b"payload")

    def fail_copystat(source, destination):
        raise OSError("metadata copy failed")

    monkeypatch.setattr(shutil, "copystat", fail_copystat)

    with pytest.raises(OSError, match="metadata copy failed"):
        await files_mod._copyfile_async(src, dest)

    assert not dest.exists()


# ---------------------------------------------------------------------------#
# Local-mode staging cleanup runs off the event loop.
#
# The staging dir holds the outputs published so far (colmap-splat PLYs,
# Neural-Canvas splats), so a synchronous ``shutil.rmtree`` freezes the loop
# for the whole delete — the heartbeat stops ticking and the CancelGuard poll
# stalls while the failure is being reported. Same bug class as the blocking
# local-mode copies; these cleanups were missed.
# ---------------------------------------------------------------------------#


@pytest.mark.asyncio
async def test_local_mode_staging_cleanup_runs_off_the_event_loop(
    tmp_path, monkeypatch,
):
    """The staging-dir rmtree must run in a worker thread, and must still
    remove the dir — identical semantics, just off the loop."""
    import shutil
    import threading
    from task_worker_api import files as files_mod

    loop_thread = threading.current_thread()
    rmtree_threads: list[threading.Thread] = []
    real_rmtree = shutil.rmtree

    def spy_rmtree(path, **kwargs):
        rmtree_threads.append(threading.current_thread())
        real_rmtree(path, **kwargs)

    monkeypatch.setattr(files_mod.shutil, "rmtree", spy_rmtree)

    shared = tmp_path / "shared"
    out_dir = tmp_path / "work" / "out"
    out_dir.mkdir(parents=True)
    (out_dir / "a.stl").write_bytes(b"aaa")
    # "b.stl" missing → the second copy raises, triggering the cleanup.

    task = _claimed(71, params={"input_path": "/ignored"})

    with pytest.raises(FileNotFoundError):
        await upload_outputs(
            task, FakeBackendClient(), _file_ctx(out_dir),
            output_files={"a": "a.stl", "b": "b.stl"},
            shared_volume_path=str(shared),
        )

    assert rmtree_threads, "staging dir was never cleaned up"
    assert all(t is not loop_thread for t in rmtree_threads), (
        "staging-dir rmtree ran on the event loop thread"
    )
    assert not (shared / "temp" / "71").exists()


# ---------------------------------------------------------------------------#
# Path-traversal guard — ``input_files`` / ``output_files`` names are joined
# into per-task sandbox dirs, so an unchecked name escaped them entirely:
# ``in_dir / "../../x"`` writes onto the worker host, ``out_dir /
# "/etc/passwd"`` drops ``out_dir`` and publishes an arbitrary container file
# to the backend, and an escaped local output name stages onto the shared
# volume beside other cases' data. Every name must be a plain basename.
# ---------------------------------------------------------------------------#


#: One escape per mechanism: relative walk-up, absolute (join discards the
#: sandbox), a nested subdirectory, the bare dot segments, a Windows
#: separator + drive-relative name (workers run on Linux, but the name comes
#: off the wire and the SDK is importable anywhere), NUL truncation, and the
#: empty name (``dir / ""`` is the dir itself).
UNSAFE_NAMES = [
    "../../x.stl",
    "../escape.stl",
    "/etc/passwd",
    "/app/.env",
    "sub/dir.stl",
    "..",
    ".",
    r"..\..\x.stl",
    "C:evil.stl",
    "%2e%2e%2f42%2fsecret.ply",
    "query?name.ply",
    "fragment#name.ply",
    "stream.ply:secret",
    "NUL",
    "con.log",
    "COM1",
    "trailing-dot.",
    "trailing-space ",
    "bad<name.ply",
    'bad"name.ply',
    "bad|name.ply",
    "bad*name.ply",
    "control\x1f.ply",
    "evil\0.stl",
    "",
]


@pytest.mark.parametrize("name", UNSAFE_NAMES)
def test_require_safe_filename_rejects_escapes(name):
    """The guard itself: every escape shape is refused, and the message
    names the offending manifest key so the failure is actionable."""
    from task_worker_api.errors import ProtocolError
    from task_worker_api.files import _require_safe_filename

    with pytest.raises(ProtocolError, match="mesh"):
        _require_safe_filename(name, field="input_files", key="mesh")


@pytest.mark.parametrize(
    "name",
    ["mesh.ply", "case_12_finalized.glb", "..hidden.stl", "a.b.c", "C_evil.stl"],
)
def test_require_safe_filename_accepts_plain_names(name):
    """Backward compatibility: ordinary filenames — including ones that
    merely start with dots or contain them — pass through unchanged."""
    from task_worker_api.files import _require_safe_filename

    assert _require_safe_filename(name, field="output_files", key="k") == name


def test_require_safe_filename_rejects_non_string():
    """A non-string name (malformed params) is refused rather than blowing
    up later inside the ``Path`` join with an opaque TypeError."""
    from task_worker_api.errors import ProtocolError
    from task_worker_api.files import _require_safe_filename

    with pytest.raises(ProtocolError, match="non-empty string"):
        _require_safe_filename(7, field="input_files", key="mesh")


@pytest.mark.asyncio
async def test_prepare_inputs_rejects_escaping_input_filename(tmp_path):
    """Remote mode: a backend-supplied ``input_files`` name that walks out
    of ``in/`` must fail the task before anything is downloaded — otherwise
    the download writes onto the worker host outside the sandbox."""
    from task_worker_api.errors import ProtocolError

    work_dir = tmp_path / "work" / "task_51"
    outside = tmp_path / "work" / "pwned.ply"

    client = FakeBackendClient()
    client.queue_file(51, "../pwned.ply", b"evil")

    task = _claimed(51, params={"input_files": {"mesh": "../pwned.ply"}})

    with pytest.raises(ProtocolError, match="input_files\\['mesh'\\]"):
        await prepare_inputs(task, client, work_dir)

    assert not outside.exists(), "input download escaped the per-task in/ dir"


@pytest.mark.asyncio
async def test_prepare_inputs_rejects_bad_name_before_downloading_any(tmp_path):
    """All-or-nothing: a bad entry anywhere in the manifest stops the whole
    batch, including the good entries listed before it."""
    from task_worker_api.errors import ProtocolError

    work_dir = tmp_path / "work" / "task_52"
    client = FakeBackendClient()
    client.queue_file(52, "good.ply", b"good")
    client.queue_file(52, "../../evil.ply", b"evil")

    task = _claimed(52, params={"input_files": {
        "mesh": "good.ply", "meta": "../../evil.ply",
    }})

    with pytest.raises(ProtocolError, match="input_files\\['meta'\\]"):
        await prepare_inputs(task, client, work_dir)

    assert not (work_dir / "in" / "good.ply").exists(), (
        "no input may be staged once the manifest is known to be unsafe"
    )
    assert not (tmp_path / "evil.ply").exists()


@pytest.mark.asyncio
async def test_prepare_inputs_rejects_case_colliding_names(tmp_path):
    """Names that differ only by case alias on Windows and must not let a
    later download overwrite an earlier logical input."""
    from task_worker_api.errors import ProtocolError

    work_dir = tmp_path / "work" / "task_56"
    client = FakeBackendClient()
    client.queue_file(56, "A.ply", b"calibration")
    client.queue_file(56, "a.ply", b"payload")
    task = _claimed(56, params={"input_files": {
        "calibration": "A.ply", "payload": "a.ply",
    }})

    with pytest.raises(ProtocolError, match="case-insensitive"):
        await prepare_inputs(task, client, work_dir)

    assert not (work_dir / "in" / "A.ply").exists()
    assert not (work_dir / "in" / "a.ply").exists()


@pytest.mark.asyncio
async def test_upload_outputs_remote_rejects_absolute_output_filename(tmp_path):
    """Remote mode: an absolute ``output_files`` name makes ``output_dir /
    name`` resolve to that absolute path, so the worker would read an
    arbitrary container file and publish it to the backend as a task
    output. It must be refused, with nothing uploaded."""
    from task_worker_api.errors import ProtocolError

    out_dir = tmp_path / "work" / "out"
    out_dir.mkdir(parents=True)
    (out_dir / "a.stl").write_bytes(b"aaa")
    secret = tmp_path / "secret.env"
    secret.write_bytes(b"API_KEY=hunter2")

    task = _claimed(53, params={"input_files": {"mesh": "in.ply"}})
    client = FakeBackendClient()

    with pytest.raises(ProtocolError, match="output_files\\['leak'\\]"):
        await upload_outputs(
            task, client, _file_ctx(out_dir),
            output_files={"a": "a.stl", "leak": str(secret)},
            shared_volume_path=None,
        )

    # All-or-nothing: not even the legitimate file ahead of it was sent.
    assert client.uploaded_files == {}, (
        "no output may be published once the manifest is known to be unsafe"
    )


@pytest.mark.asyncio
async def test_upload_outputs_rejects_case_colliding_names(tmp_path):
    """Two logical outputs must not alias the same file on Windows."""
    from task_worker_api.errors import ProtocolError

    out_dir = tmp_path / "work" / "out"
    out_dir.mkdir(parents=True)
    task = _claimed(57, params={"input_files": {"mesh": "in.ply"}})
    client = FakeBackendClient()

    with pytest.raises(ProtocolError, match="case-insensitive"):
        await upload_outputs(
            task, client, _file_ctx(out_dir),
            output_files={"preview": "Result.ply", "mesh": "result.ply"},
            shared_volume_path=None,
        )

    assert client.uploaded_files == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["remote", "local", "no-shared-volume"])
async def test_upload_outputs_rejects_symlink_sources(tmp_path, mode):
    """A plain basename must not smuggle an outside file through a symlink."""
    from task_worker_api.errors import ProtocolError

    out_dir = tmp_path / "work" / "out"
    out_dir.mkdir(parents=True)
    secret = tmp_path / "secret.env"
    secret.write_bytes(b"API_KEY=not-for-upload")
    link = out_dir / "result.bin"
    try:
        link.symlink_to(secret)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    client = FakeBackendClient()
    if mode == "remote":
        params = {"input_files": {"mesh": "in.ply"}}
        shared_volume_path = None
    elif mode == "local":
        params = {"input_path": "/ignored"}
        shared_volume_path = str(tmp_path / "shared")
    else:
        params = {"input_path": "/ignored"}
        shared_volume_path = None

    with pytest.raises(ProtocolError, match="symbolic link"):
        await upload_outputs(
            _claimed(58, params=params), client, _file_ctx(out_dir),
            output_files={"artifact": "result.bin"},
            shared_volume_path=shared_volume_path,
        )

    assert client.uploaded_files == {}
    assert not (tmp_path / "shared" / "temp" / "58").exists()


@pytest.mark.asyncio
async def test_upload_outputs_local_rejects_escaping_output_filename(tmp_path):
    """Local mode: an output name that walks out of the staging dir would
    write onto the shared volume beside other cases' data. It must be
    refused before the staging dir is even created."""
    from task_worker_api.errors import ProtocolError

    shared = tmp_path / "shared"
    out_dir = tmp_path / "work" / "out"
    out_dir.mkdir(parents=True)
    (out_dir / "a.stl").write_bytes(b"aaa")
    (out_dir / "escape.stl").write_bytes(b"eee")

    task = _claimed(54, params={"input_path": "/ignored"})

    with pytest.raises(ProtocolError, match="output_files\\['b'\\]"):
        await upload_outputs(
            task, FakeBackendClient(), _file_ctx(out_dir),
            output_files={"a": "a.stl", "b": "../escape.stl"},
            shared_volume_path=str(shared),
        )

    assert not (shared / "temp" / "escape.stl").exists(), (
        "output copy escaped the per-task staging dir"
    )
    assert not (shared / "temp" / "54").exists()


@pytest.mark.asyncio
async def test_upload_outputs_no_shared_volume_rejects_escaping_name(tmp_path):
    """The no-shared-volume branch returns ``output_dir / name`` paths
    straight into the result manifest, so it needs the same guard: an
    escaping name must not be handed to the backend as a task output."""
    from task_worker_api.errors import ProtocolError

    out_dir = tmp_path / "work" / "out"
    out_dir.mkdir(parents=True)

    task = _claimed(55, params={"input_path": "/ignored"})

    with pytest.raises(ProtocolError, match="output_files\\['a'\\]"):
        await upload_outputs(
            task, FakeBackendClient(), _file_ctx(out_dir),
            output_files={"a": "../../etc/passwd"},
            shared_volume_path=None,
        )
