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
