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

Every ``input_files`` / ``output_files`` name is checked by
:func:`_require_safe_filename` before it is joined into a per-task
directory — those names come from backend task params and handler
output manifests, so they are the file-transfer trust boundary.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import ntpath
import os
import shutil
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from .context import ClaimedTask, FileContext
from .errors import ProtocolError, TaskCancelled

if TYPE_CHECKING:  # pragma: no cover
    from .client import BackendClient

log = logging.getLogger(__name__)

#: Bytes moved per ``_copyfile_async`` iteration. Each filesystem operation is
#: offloaded from the event-loop thread; chunking adds cancellation points
#: without making a multi-GB copy thread-dispatch-bound.
_COPY_CHUNK_BYTES = 4 * 1024 * 1024


#: Never legal in a task filename: path separators, URL delimiters/escapes,
#: Windows-illegal characters, and NUL. The URL characters matter because
#: filenames are interpolated into ``/tasks/{id}/files/{filename}``: an
#: already-escaped ``%2f`` would otherwise become a separator at the backend.
_FILENAME_REJECTED_CHARS = frozenset('/\\\0<>:"|?*%#')

#: Windows resolves these names to devices even when an extension is present
#: (for example ``NUL.log``). Reject them on every platform so a manifest has
#: the same meaning on Linux and Windows workers.
_WINDOWS_RESERVED_FILENAMES = (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def _require_safe_filename(value: Any, *, field: str, key: str) -> str:
    """Return ``value`` if it is a plain filename; raise ``ProtocolError`` if not.

    ``input_files`` values arrive in the backend's task params and
    ``output_files`` values come from the handler's result manifest; both
    are joined into a per-task directory (``work_dir/in``, ``work_dir/out``,
    ``shared_volume_path/temp/<task_id>``). ``Path`` join has no notion of a
    sandbox: ``in_dir / "../../x"`` walks out of it, and
    ``out_dir / "/etc/passwd"`` discards ``out_dir`` entirely. Unchecked,
    that let a task read or write anywhere the worker process can — an
    escaped input write lands on the worker host, an escaped local output
    stages onto the shared volume next to other cases' patient data, and an
    absolute remote output name publishes an arbitrary container file
    (env files, credentials) to the backend as a task output.

    So a name must be exactly one path component. ``field``/``key`` name the
    offending manifest entry in the message: these fail a task that a
    handler or the backend has to fix, and "some filename was bad" is not
    an actionable failure reason.
    """
    if not isinstance(value, str) or not value:
        problem = "must be a non-empty string"
    elif value in (".", ".."):
        problem = "must not be '.' or '..'"
    elif ntpath.splitdrive(value)[0]:
        # "C:x" has no separator but is drive-relative, so a Windows join
        # still escapes. Checked with ntpath explicitly: workers run in
        # Linux containers, where os.path would wave it through.
        problem = "must not carry a drive or UNC prefix"
    elif any(ch in _FILENAME_REJECTED_CHARS for ch in value):
        problem = "contains a path, URL, or platform-reserved character"
    elif any(ord(ch) < 32 for ch in value):
        problem = "must not contain control characters"
    elif value.endswith((".", " ")):
        problem = "must not end with a dot or space"
    elif value.split(".", 1)[0].rstrip(" .").upper() in _WINDOWS_RESERVED_FILENAMES:
        problem = "is a Windows reserved device name"
    else:
        return value

    raise ProtocolError(
        f"{field}[{key!r}] = {value!r} is not a safe filename: {problem}. "
        "Task file names are joined into the per-task sandbox directory, so "
        "they must be plain filenames with no directory component."
    )


def _require_safe_filenames(
    values: dict[str, Any], *, field: str,
) -> dict[str, str]:
    """Validate one manifest and reject cross-platform filename aliases."""
    safe: dict[str, str] = {}
    aliases: dict[str, str] = {}
    for key, value in values.items():
        filename = _require_safe_filename(value, field=field, key=key)
        alias = ntpath.normcase(filename)
        previous = aliases.get(alias)
        if previous is not None and previous != filename:
            raise ProtocolError(
                f"{field}[{key!r}] = {filename!r} aliases {previous!r} on "
                "case-insensitive filesystems. Task file names must be "
                "distinct on both Linux and Windows."
            )
        aliases[alias] = filename
        safe[key] = filename
    return safe


def _require_output_sources(
    output_dir: Path, output_files: dict[str, str],
) -> dict[str, tuple[str, Path]]:
    """Reject unsafe existing output sources without following symlinks."""
    root = output_dir.resolve(strict=True)
    sources: dict[str, tuple[str, Path]] = {}
    for key, filename in output_files.items():
        src = output_dir / filename
        try:
            source_stat = src.lstat()
        except FileNotFoundError:
            # Preserve each publish branch's existing missing-file behavior:
            # remote/local publishing raises when it tries to read/copy, while
            # no-shared-volume mode only returns the declared manifest path.
            sources[key] = (filename, src)
            continue
        if stat.S_ISLNK(source_stat.st_mode):
            problem = "must not be a symbolic link"
        elif not stat.S_ISREG(source_stat.st_mode):
            problem = "must be a regular file"
        else:
            resolved = src.resolve(strict=True)
            try:
                resolved.relative_to(root)
            except ValueError:
                problem = "resolves outside the task output directory"
            else:
                sources[key] = (filename, src)
                continue

        raise ProtocolError(
            f"output_files[{key!r}] = {filename!r} is not a safe output: "
            f"{problem}. Task outputs must be regular files created directly "
            "inside the task output directory."
        )
    return sources


async def _copyfile_async(
    src: Path,
    dest: Path,
    *,
    cancelled: Optional[asyncio.Event] = None,
    cancel_message: str = "cancelled by user during file copy",
) -> None:
    """``shutil.copy2`` equivalent with cancellable, off-loop I/O.

    ``shutil.copy2`` is one blocking call: a multi-GB copy (Blender-CLI
    ``.blend`` inputs, Neural-Canvas splats on a network-mounted shared
    volume) freezes the event loop for its whole duration. The heartbeat
    stops ticking — so the backend's stale-task sweeper can reclaim a task
    the worker is actively copying in — and the ``CancelGuard`` poll
    freezes, so a user cancel stays invisible until the copy finishes.

    Opening, reading, writing, closing, and copying metadata all run through
    :func:`asyncio.to_thread`, so a slow filesystem operation cannot block the
    event loop. Chunk boundaries let ``cancelled`` abort mid-file instead of
    only between files.

    Metadata and error semantics match ``copy2``: ``copystat`` runs on
    success, and a missing/unreadable ``src`` raises ``FileNotFoundError``
    from ``open`` exactly as before. A same-file copy raises
    :class:`shutil.SameFileError`. If this invocation opens ``dest`` for
    writing, any partial destination is removed before an exception
    propagates, mirroring the ``BackendClient.download_file`` cleanup
    contract. Failures before that point leave a pre-existing destination
    untouched.
    """
    fsrc = None
    fdst = None
    dest_touched = False
    cancelled_before_read = (
        cancelled is not None and cancelled.is_set()
    )

    def _samefile() -> bool:
        return dest.exists() and os.path.samefile(src, dest)

    try:
        if await asyncio.to_thread(_samefile):
            raise shutil.SameFileError(
                f"{src!r} and {dest!r} are the same file"
            )

        fsrc = await asyncio.to_thread(open, src, "rb")
        try:
            fdst = await asyncio.to_thread(open, dest, "wb")
            dest_touched = True
            while True:
                chunk = await asyncio.to_thread(fsrc.read, _COPY_CHUNK_BYTES)
                if not chunk:
                    break
                # Apply the state sampled before this read only after a
                # non-empty chunk returns. This matches the old synchronous
                # first iteration, while an exhausted source still wins over
                # a cancel detected after its final write.
                if cancelled_before_read:
                    raise TaskCancelled(cancel_message)
                await asyncio.to_thread(fdst.write, chunk)
                cancelled_before_read = (
                    cancelled is not None and cancelled.is_set()
                )
        finally:
            try:
                if fdst is not None:
                    await asyncio.to_thread(fdst.close)
            finally:
                await asyncio.to_thread(fsrc.close)

        await asyncio.to_thread(shutil.copystat, src, dest)
    except BaseException:
        if dest_touched:
            # A partial copy would otherwise be picked up as a complete input
            # by a retried task, or linger in the output staging directory.
            try:
                await asyncio.to_thread(dest.unlink)
            except OSError:
                pass
        raise


#: One WARNING per process when a client predating the ``cancelled`` keyword
#: is used with a live cancel event — the condition is per-transfer, and a
#: line per file of a multi-file batch would be noise.
_warned_legacy_cancel_kwarg = False


def _cancel_kwarg(
    fn: Any, cancelled: Optional[asyncio.Event], *, transfer: str,
) -> dict:
    """``{"cancelled": event}`` if ``fn`` takes it, else ``{}``.

    ``cancelled=`` was added to :meth:`BackendClient.download_file` and
    :meth:`BackendClient.upload_file` after worker repos had already grown
    their own clients, test doubles and ``FakeBackendClient`` subclasses
    against the pre-existing positional signatures (``Worker(client=...)``
    takes any duck-typed client). Passing the keyword unconditionally would
    raise ``TypeError`` on every one of them the moment a cancel guard is
    active — which is always, since the worker starts one before staging
    inputs. So the keyword only goes to callees that declare it (or accept
    ``**kwargs``); older ones keep the pre-existing behaviour, where a
    cancel is noticed between files rather than mid-stream.

    ``transfer`` names the operation ("remote input download" / "output
    upload") in the one-time warning.
    """
    global _warned_legacy_cancel_kwarg
    if cancelled is None:
        return {}
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):  # pragma: no cover — unintrospectable
        # Can't tell; assume the current signature rather than silently
        # dropping cancellation for a client that does support it.
        return {"cancelled": cancelled}
    if "cancelled" in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    ):
        return {"cancelled": cancelled}
    if not _warned_legacy_cancel_kwarg:
        _warned_legacy_cancel_kwarg = True
        log.warning(
            "%s has no 'cancelled' parameter; a user cancel during a %s "
            "will not be noticed until the file finishes streaming. "
            "Add 'cancelled: Optional[asyncio.Event] = None' as a "
            "keyword-only parameter and forward it to abort mid-stream.",
            getattr(fn, "__qualname__", fn), transfer,
        )
    return {}


async def prepare_inputs(
    task: ClaimedTask, client: "BackendClient", work_dir: Path,
    *,
    cancelled: Optional[asyncio.Event] = None,
) -> FileContext:
    """Materialise task inputs under ``work_dir/in/``. Returns a FileContext.

    When ``cancelled`` is supplied (an ``asyncio.Event`` from a CancelGuard
    started before this call), remote-mode batch downloads abort as soon as
    the event is set: a multi-file input set for a GB-scale task can spend
    minutes streaming, and a user cancel during that window should not wait
    for every remaining file to finish downloading. The event is checked
    between files *and* handed to ``download_file``, which checks it between
    chunk writes — so a cancel aborts mid-file too. That matters most for a
    single-file input set (a lone colmap-splat PLY, a Neural-Canvas splat),
    where the between-files check never fires and the whole multi-GB stream
    would otherwise run to completion after the cancel. A client whose
    ``download_file`` predates the ``cancelled`` keyword still works — see
    :func:`_download_cancel_kwarg` — it just keeps the between-files-only
    cancellation it always had. Local mode
    (``input_path``) honours it the same way: the copy runs through
    :func:`_copyfile_async`, which checks the event between chunks, so a
    cancel aborts mid-file rather than after a multi-GB copy completes.

    Remote-mode ``input_files`` names are backend-supplied and land under
    ``work_dir/in/``, so each must be a plain basename; one that isn't
    fails the task with a :class:`ProtocolError` naming its key, before any
    download starts. See :func:`_require_safe_filename`.
    """
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
        await _copyfile_async(
            src, dest, cancelled=cancelled,
            cancel_message=(
                f"task {task.id} cancelled by user during input copy"
            ),
        )
        return FileContext(
            input_dir=in_dir,
            output_dir=out_dir,
            primary_path=dest,
            all_paths={"mesh": dest},
        )

    if input_files:
        # Validate the whole set before the first download: a name that
        # escapes ``in_dir`` must fail the task, not write onto the worker
        # host, and failing up front also avoids leaving a half-staged
        # input dir behind for the sake of a manifest we already know is bad.
        safe_input_files = _require_safe_filenames(
            input_files, field="input_files",
        )
        paths: dict[str, Path] = {}
        for key, filename in safe_input_files.items():
            if cancelled is not None and cancelled.is_set():
                raise TaskCancelled(
                    f"task {task.id} cancelled by user during input download"
                )
            dest = in_dir / filename
            await client.download_file(
                task.id, filename, dest,
                **_cancel_kwarg(
                    client.download_file, cancelled,
                    transfer="remote input download",
                ),
            )
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
    *,
    cancelled: Optional[asyncio.Event] = None,
) -> dict[str, str]:
    """Publish output_files and return the manifest for task.result.

    ``output_files`` is ``{logical_key: filename}`` produced by the handler;
    return map is either filenames (remote mode) or absolute paths
    (local mode).

    When ``cancelled`` is supplied (an ``asyncio.Event`` from a CancelGuard
    that stays active through the upload phase), remote-mode batch uploads
    abort as soon as the event is set: uploading GB-scale outputs
    (colmap-splat PLY files, Neural-Canvas splats) can take minutes, and a
    user cancel during that window should not wait for every remaining file
    to finish streaming to a task the user already cancelled. The check
    runs between files, raising ``TaskCancelled`` before the next upload
    starts — mirroring the cancel-during-download guard in
    :func:`prepare_inputs`. The event is also handed to ``upload_file``
    (when the client declares the ``cancelled`` keyword), which aborts a
    cancel mid-stream too: a single-file output set — a lone colmap-splat
    PLY, a Neural-Canvas splat — has no between-files boundary, so without
    the in-stream abort the whole multi-GB body would stream to completion
    after the cancel. Local-mode staging copies go through
    :func:`_copyfile_async`, so the event is also checked *within* a file:
    a cancel aborts mid-copy instead of waiting out a multi-GB write.

    If publishing fails partway through (the Nth upload/copy raises after
    files 1..N-1 succeeded), the partial artifacts are removed before the
    exception propagates: a failed task is retried, and the half-published
    outputs from the failed attempt would otherwise be orphans the
    backend's completed-task sweeper never reaches (it only sweeps staging
    dirs / accepts output manifests for tasks it recorded as complete).
    Mirrors the ``BackendClient.download_file`` partial-file cleanup.

    Every filename in ``output_files`` must be a plain basename; one that
    isn't fails the task with a :class:`ProtocolError` naming its key. The
    whole manifest is checked before the first upload or copy, so a bad
    entry can't publish the entries ahead of it first — an all-or-nothing
    check keeps a rejected manifest from leaving artifacts behind in the
    staging dir or on the backend.
    """
    safe_output_files = _require_safe_filenames(
        output_files, field="output_files",
    )
    output_sources = _require_output_sources(
        file_ctx.output_dir, safe_output_files,
    )

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
            for _, (filename, src) in output_sources.items():
                if cancelled is not None and cancelled.is_set():
                    raise TaskCancelled(
                        f"task {task.id} cancelled by user during output upload"
                    )
                await client.upload_file(
                    task.id, filename, src,
                    **_cancel_kwarg(
                        client.upload_file, cancelled,
                        transfer="output upload",
                    ),
                )
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
        return dict(safe_output_files)

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
            for key, (filename, src) in output_sources.items():
                if cancelled is not None and cancelled.is_set():
                    raise TaskCancelled(
                        f"task {task.id} cancelled by user during output upload"
                    )
                dest = dest_dir / filename
                await _copyfile_async(
                    src, dest, cancelled=cancelled,
                    cancel_message=(
                        f"task {task.id} cancelled by user during output copy"
                    ),
                )
                manifest[key] = str(dest)
        except Exception:
            # A copy failed partway through — the staging dir holds a
            # subset of the outputs. Remove the whole staging dir so a
            # retried task starts clean and no orphaned partial artifacts
            # confuse the backend's sweep. The backend only sweeps staging
            # dirs for tasks it recorded as complete; a failed task's dir
            # would otherwise linger indefinitely.
            #
            # Off-loop for the same reason the copies themselves are: the dir
            # can hold GB-scale artifacts (colmap-splat PLYs, Neural-Canvas
            # splats), and unlinking them synchronously would freeze the
            # heartbeat and the CancelGuard poll while the failure is being
            # reported. ``ignore_errors=True`` keeps this non-raising.
            await asyncio.to_thread(shutil.rmtree, dest_dir, ignore_errors=True)
            raise
        return manifest

    return {
        key: str(src)
        for key, (_, src) in output_sources.items()
    }
