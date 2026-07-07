"""File transfer with local/remote auto-detection.

Local mode — ``task.params.input_path`` points at a file on a shared
volume that the worker can read directly. File is copied into the
worker's per-task input dir so handlers never mutate the shared source.

Remote mode — ``task.params.input_files`` is a ``{key: filename}`` map;
each is streamed via ``GET /tasks/{id}/files/{name}``.

The same file supplies output publishing: local mode copies to
``shared_volume_path/temp/<task_id>/`` (a short-lived staging dir the
backend consumer is expected to sweep after it moves the artifacts to
their final location); remote mode PUTs each via HTTP.

Both modes clean up partial artifacts if publishing fails partway
through: a failed task is retried, and the partially-published outputs
from the failed attempt (a half-populated staging dir, or a subset of
uploaded files on the backend) would otherwise linger as orphans that
the backend's completed-task sweeper never reaches. Mirrors the
``BackendClient.download_file`` partial-file cleanup contract.
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from .context import ClaimedTask, FileContext

if TYPE_CHECKING:  # pragma: no cover
    from .client import BackendClient

log = logging.getLogger(__name__)


async def prepare_inputs(
    task: ClaimedTask, client: "BackendClient", work_dir: Path,
) -> FileContext:
    """Materialise task inputs under ``work_dir/in/``. Returns a FileContext."""
    in_dir = work_dir / "in"
    out_dir = work_dir / "out"
    in_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    params = task.params or {}
    input_path = params.get("input_path")
    input_files = params.get("input_files")

    if input_path:
        src = Path(input_path)
        if not src.is_file():
            raise FileNotFoundError(f"input_path not accessible: {src}")
        dest = in_dir / src.name
        shutil.copy2(src, dest)
        return FileContext(
            input_dir=in_dir,
            output_dir=out_dir,
            primary_path=dest,
            all_paths={"mesh": dest},
        )

    if input_files:
        paths: dict[str, Path] = {}
        for key, filename in input_files.items():
            dest = in_dir / filename
            await client.download_file(task.id, filename, dest)
            paths[key] = dest
        primary_key = "mesh" if "mesh" in paths else next(iter(paths))
        return FileContext(
            input_dir=in_dir,
            output_dir=out_dir,
            primary_path=paths[primary_key],
            all_paths=paths,
        )

    # No input files declared — some task types (pure-param work) don't
    # need any. Return empty input dir with output dir ready.
    return FileContext(
        input_dir=in_dir,
        output_dir=out_dir,
        primary_path=in_dir,  # sentinel: no primary file
        all_paths={},
    )


async def upload_outputs(
    task: ClaimedTask,
    client: "BackendClient",
    file_ctx: FileContext,
    output_files: dict[str, str],
    shared_volume_path: "str | None",
) -> dict[str, str]:
    """Publish output_files and return the manifest for task.result.

    ``output_files`` is ``{logical_key: filename}`` produced by the handler;
    return map is either filenames (remote mode) or absolute paths
    (local mode).

    If publishing fails partway through (the Nth upload/copy raises after
    files 1..N-1 succeeded), the partial artifacts are removed before the
    exception propagates: a failed task is retried, and the half-published
    outputs from the failed attempt would otherwise be orphans the
    backend's completed-task sweeper never reaches (it only sweeps staging
    dirs / accepts output manifests for tasks it recorded as complete).
    Mirrors the ``BackendClient.download_file`` partial-file cleanup.
    """
    remote_mode = bool((task.params or {}).get("input_files"))

    if remote_mode:
        # Track filenames as they upload so a failure partway through can
        # remove the already-delivered ones before re-raising. There is no
        # backend "delete output file" endpoint, so best-effort here means
        # re-uploading on retry — but we at least surface the partial state
        # in the logs so an operator can reconcile. The exception still
        # propagates so the task is marked failed and retried cleanly.
        uploaded: list[str] = []
        try:
            for _, filename in output_files.items():
                src = file_ctx.output_dir / filename
                await client.upload_file(task.id, filename, src)
                uploaded.append(filename)
        except Exception:
            if uploaded:
                log.warning(
                    "upload_outputs for task %s failed after publishing %d "
                    "output file(s) (%s); these partial uploads remain on "
                    "the backend and will be overwritten on retry.",
                    task.id, len(uploaded), ", ".join(uploaded),
                )
            raise
        return dict(output_files)

    if shared_volume_path:
        # Staging dir for local-mode outputs. Lives under ``temp/`` so
        # (a) workers don't pollute the shared volume root with one
        # ``{task_id}/`` folder per completed task, and (b) the backend
        # mirror has an obvious place to rmdir once it has moved the
        # artifacts to their permanent home.
        dest_dir = Path(shared_volume_path) / "temp" / str(task.id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        manifest: dict[str, str] = {}
        try:
            for key, filename in output_files.items():
                src = file_ctx.output_dir / filename
                dest = dest_dir / filename
                shutil.copy2(src, dest)
                manifest[key] = str(dest)
        except Exception:
            # A copy failed partway through — the staging dir holds a
            # subset of the outputs. Remove the whole staging dir so a
            # retried task starts clean and no orphaned partial artifacts
            # confuse the backend's sweep. The backend only sweeps staging
            # dirs for tasks it recorded as complete; a failed task's dir
            # would otherwise linger indefinitely.
            shutil.rmtree(dest_dir, ignore_errors=True)
            raise
        return manifest

    return {
        key: str(file_ctx.output_dir / filename)
        for key, filename in output_files.items()
    }
