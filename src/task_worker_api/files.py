"""File transfer with local/remote auto-detection.

Local mode — ``task.params.input_path`` points at a file on a shared
volume that the worker can read directly. File is copied into the
worker's per-task input dir so handlers never mutate the shared source.

Remote mode — ``task.params.input_files`` is a ``{key: filename}`` map;
each is streamed via ``GET /tasks/{id}/files/{name}``.

The same file supplies output publishing: local mode copies to
``shared_volume_path/temp/<task_id>/`` (a short-lived staging dir the
backend consumer is expected to sweep after it moves the artifacts to
their final location); remote mode PUTs each via HTTP. Every attempt of a
task stages into that one directory, so a local publish goes to a scratch
name and is renamed onto the published one — see :func:`_stage_output`.

A rename keeps two attempts from splicing one file, but it does not keep
the published name pointing at the attempt that last renamed onto it: a
successor's publish replaces it whole, and the predecessor's manifest still
names that path. So each local publish also records the identity of the
artifact it committed, and :func:`_unowned_outputs` re-checks it — an
attempt whose outputs a successor has taken over must not report them as
its results (``Worker._run_one`` reports the failure instead).

Neither mode ever deletes a published artifact. An attempt that fails —
partway through publishing, or after publishing everything (a late cancel,
a watchdog deadline, an unencodable result — see ``Worker._run_one``) —
removes only its own uniquely named ``.part`` scratch file, and its caller
logs what it left behind; see :func:`_warn_published_orphans`.

An attempt that is *killed* (SIGKILL, the watchdog's ``os._exit``) runs no
cleanup at all, so its scratch file outlives it and would keep the staging
dir non-empty forever — the consumer's non-recursive ``rmdir`` never
succeeds, and the directory the ``temp/`` layout exists to avoid leaks once
per killed publish. Scratch files are therefore written under an exclusive
``flock`` the kernel drops on process death, and every local publish first
reclaims the ones whose lock it can take: dead predecessors' residue goes,
a live successor's file stays. See :func:`_open_scratch` and
:func:`_sweep_dead_scratch`.

Reclaiming those orphans is deferred, deliberately. ``temp/<task_id>`` is
shared by every attempt of the task, and a failing attempt cannot prove the
file at a published name is still the one it wrote: the backend can already
have re-queued the task (the watchdog's synchronous ``fail`` lands from its
own thread while the event loop is wedged; the stale-task sweeper needs no
report at all) and a successor can already have published there. Any
identity check the SDK could make is a stat, and POSIX has no
compare-and-unlink, so the successor can still land in the gap. Doing this
safely needs a staging path unique per attempt that the backend consumers
understand, or a lease on the shared one — neither exists yet, and until
one does the orphan is logged and left in place, because deleting a live
attempt's outputs is far worse than leaving a stale file for an operator.

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
import re
import shutil
import stat
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover — non-POSIX; workers run on Linux
    fcntl = None  # type: ignore[assignment]

from .context import ClaimedTask, FileContext
from .errors import CrossBoxLocalModeError, ProtocolError, TaskCancelled

if TYPE_CHECKING:  # pragma: no cover
    from .client import BackendClient

log = logging.getLogger(__name__)

#: The scratch names :func:`_stage_output` publishes through:
#: ``.<uuid4 hex>.part``. Matched exactly, because :func:`_sweep_dead_scratch`
#: unlinks what it matches and a published output may legitimately start with
#: a dot.
_SCRATCH_NAME_RE = re.compile(r"\.[0-9a-f]{32}\.part")

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


#: Transfer directions already warned about, so a client predating the
#: ``cancelled`` keyword logs one WARNING per process per direction — the
#: condition is per-file, and a line per file of a multi-file batch would be
#: noise.
_warned_legacy_transfers: set[str] = set()


def _cancel_kwarg(
    transfer: Any, cancelled: Optional[asyncio.Event], *, phase: str,
) -> dict:
    """``{"cancelled": event}`` if ``transfer`` takes it, else ``{}``.

    ``cancelled=`` was added to :meth:`BackendClient.download_file` and
    :meth:`BackendClient.upload_file` after worker repos had already grown
    their own clients, test doubles and ``FakeBackendClient`` subclasses
    against the three-positional-argument signatures (``Worker(client=...)``
    takes any duck-typed client). Passing the keyword unconditionally would
    raise ``TypeError`` on every one of them the moment a cancel guard is
    active — which is always, since the worker keeps one running from before
    inputs are staged until after outputs are published. So the keyword only
    goes to callees that declare it (or accept ``**kwargs``); older ones keep
    the pre-existing behaviour, where a cancel is noticed between files rather
    than mid-stream.

    ``phase`` names the direction ("remote input download" / "remote output
    upload") in the one-time warning.
    """
    if cancelled is None:
        return {}
    try:
        params = inspect.signature(transfer).parameters
    except (TypeError, ValueError):  # pragma: no cover — unintrospectable
        # Can't tell; assume the current signature rather than silently
        # dropping cancellation for a client that does support it.
        return {"cancelled": cancelled}
    if "cancelled" in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    ):
        return {"cancelled": cancelled}
    if phase not in _warned_legacy_transfers:
        _warned_legacy_transfers.add(phase)
        log.warning(
            "%s has no 'cancelled' parameter; a user cancel during a %s will "
            "not be noticed until the file finishes streaming. Add "
            "'cancelled: Optional[asyncio.Event] = None' as a keyword-only "
            "parameter and forward it to abort mid-stream.",
            getattr(transfer, "__qualname__", transfer), phase,
        )
    return {}


#: Params keys that only make sense on the task's home box (absolute paths
#: on ITS shared volume). A foreign claim must never resolve these locally:
#: every box shares the same ``/app/shared`` layout and case ids collide
#: across databases, so the path can exist AND be the wrong box's file.
_LOCAL_ONLY_PARAM_KEYS = ("output_dir", "snapshot_path")


def _require_foreign_capable(task: ClaimedTask) -> None:
    """Raise :class:`CrossBoxLocalModeError` unless this task can run remotely.

    Remote-capable means: inputs are declared via ``input_files`` (or the task
    is pure-param) and no local-only path key is present. ``input_path`` on its
    own is a shared-volume contract; resolving it against a foreign worker's
    identically-laid-out volume would silently process the wrong box's patient
    file, so refusal happens here — before any filesystem access.
    """
    params = task.params or {}
    local_only = [k for k in _LOCAL_ONLY_PARAM_KEYS if params.get(k)]
    if local_only:
        raise CrossBoxLocalModeError(
            f"cross-box claim refused: task {task.id} ({task.task_type.value}) "
            f"carries shared-volume-only params {local_only}; this task type "
            "is not remote-capable. Remove it from this worker's cross-box "
            "key on the target box."
        )
    if params.get("input_path") and not params.get("input_files"):
        raise CrossBoxLocalModeError(
            f"cross-box claim refused: task {task.id} ({task.task_type.value}) "
            "is shared-volume (local-mode) only — it has input_path but no "
            "input_files. Either the target box has not enabled "
            "ENABLE_CROSS_BOX_FILES (drain pre-flag tasks first), or this "
            "task type was granted to a cross-box key without being "
            "remote-capable."
        )


async def prepare_inputs(
    task: ClaimedTask, client: "BackendClient", work_dir: Path,
    *,
    cancelled: Optional[asyncio.Event] = None,
    foreign: bool = False,
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
    :func:`_cancel_kwarg` — it just keeps the between-files-only
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

    if foreign:
        # A task claimed from a non-home target must never resolve
        # shared-volume paths against THIS worker's volume (wrong-box
        # hazard); refuse local-only tasks, and for dual-param tasks use
        # only the HTTP input_files channel.
        _require_foreign_capable(task)
        input_path = None

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
                    phase="remote input download",
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
    foreign: bool = False,
    staged: "list[str] | None" = None,
    owned: "dict[str, tuple[int, int]] | None" = None,
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
    to finish streaming to a task the user already cancelled. The event is
    checked between files *and* handed to ``upload_file``, which races it
    against the in-flight PUT — so a cancel aborts mid-file too. That
    matters most for a single-file output set (a lone colmap-splat PLY, a
    Neural-Canvas splat), where the between-files check never fires and the
    whole multi-GB body would otherwise stream to completion after the
    cancel. A client whose ``upload_file`` predates the ``cancelled``
    keyword still works — see :func:`_cancel_kwarg` — it just keeps the
    between-files-only cancellation it always had. Local-mode staging copies
    go through :func:`_copyfile_async`, so the event is also checked *within*
    a file: a cancel aborts mid-copy instead of waiting out a multi-GB write.

    If publishing fails partway through (the Nth upload/copy raises after
    files 1..N-1 succeeded), files 1..N-1 stay where they are and the
    exception propagates. Only the failing copy's own ``.part`` scratch file
    is removed — that name is this call's alone, so removing it is provably
    safe, and leaving one behind would keep the staging dir non-empty and
    defeat the backend consumer's ``rmdir``. Nothing already published is
    touched: the staging path is shared by every attempt of the task, and
    this attempt cannot prove the file at a published name is still its own.

    A killed attempt cleans up nothing, so before staging anything this
    reclaims the scratch files left in ``temp/<task_id>`` by attempts that no
    longer exist — proven by their ``flock`` being free — and leaves a live
    concurrent attempt's alone. That is what keeps the staging dir sweepable
    by the consumer's ``rmdir`` across a mid-publish kill; see
    :func:`_sweep_dead_scratch`.

    Warning about those leftovers is the *caller's* job, not this function's:
    a partial publish fails the attempt, and the attempt's terminal path
    already warns about everything it published (``Worker._run_one``, via
    :func:`_warn_published_orphans`) — including whatever this call recorded
    in ``staged`` before it raised. Warning here as well emitted the same
    orphan list twice for one failure.

    ``staged``, when supplied, is appended to in place as each artifact
    leaves this attempt's workdir — staged paths in local mode, filenames in
    remote mode. It is what the caller needs to name the orphans if the
    attempt still ends failed (see ``Worker._run_one``), not a delete list.

    ``owned``, when supplied, is filled in the same way with
    ``{staged path: (st_dev, st_ino)}`` for local-mode publishes — the
    identity of the artifact *this* attempt put at each published name, read
    off the scratch file the rename commits (see :func:`_stage_output`).
    Because ``temp/<task_id>`` is shared by every attempt of a task, a
    concurrent attempt can replace a name this one published; the rename
    keeps that from splicing bytes, but it does not keep the name pointing at
    this attempt's artifact. Passing ``owned`` and re-checking it with
    :func:`_unowned_outputs` before reporting is what lets a caller notice
    that its manifest now describes someone else's file. Remote mode fills
    nothing: there is no local artifact to identify.

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

    # Publish over HTTP when the task was claimed from a foreign box, or when
    # the task is input_files-only (a genuinely volume-less worker). A
    # dual-param task (input_path + input_files, the cross-box enqueue shape)
    # claimed by a HOME worker stays on the shared-volume path — presence of
    # input_files alone must not flip a co-located fleet to HTTP uploads.
    params = task.params or {}
    remote_mode = foreign or (
        bool(params.get("input_files")) and not params.get("input_path")
    )
    # Caller-visible when supplied, throwaways otherwise, so the publish
    # loops below have one place to record what they published either way.
    staged = [] if staged is None else staged
    owned = {} if owned is None else owned

    if remote_mode:
        # Track filenames as they upload so a failure partway through leaves
        # the caller able to name the already-delivered ones. There is no
        # backend "delete output file" endpoint, so all anyone can do is
        # surface the partial state in the logs for an operator to reconcile;
        # a retry re-uploads over them. The exception propagates so the task
        # is marked failed and retried cleanly.
        for _, (filename, src) in output_sources.items():
            if cancelled is not None and cancelled.is_set():
                raise TaskCancelled(
                    f"task {task.id} cancelled by user during output upload"
                )
            await client.upload_file(
                task.id, filename, src,
                **_cancel_kwarg(
                    client.upload_file, cancelled,
                    phase="remote output upload",
                ),
            )
            staged.append(filename)
        return dict(safe_output_files)

    if shared_volume_path:
        # Staging dir for local-mode outputs. Lives under ``temp/`` so
        # (a) workers don't pollute the shared volume root with one
        # ``{task_id}/`` folder per completed task, and (b) the backend
        # mirror has an obvious place to rmdir once it has moved the
        # artifacts to their permanent home.
        dest_dir = Path(shared_volume_path) / "temp" / str(task.id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        # Off the loop: it stats and unlinks on the shared volume, which can
        # be a slow mount, and nothing here is cancellation-critical — no
        # artifact has been published yet.
        await asyncio.to_thread(_sweep_dead_scratch, dest_dir)
        manifest: dict[str, str] = {}
        for key, (filename, src) in output_sources.items():
            if cancelled is not None and cancelled.is_set():
                raise TaskCancelled(
                    f"task {task.id} cancelled by user during output upload"
                )
            dest = dest_dir / filename
            ident = await _stage_output(
                src, dest, cancelled=cancelled,
                cancel_message=(
                    f"task {task.id} cancelled by user during output copy"
                ),
            )
            # No await between the rename and these two records, so a publish
            # is either not done or done, recorded, and identified.
            staged.append(str(dest))
            owned[str(dest)] = ident
            manifest[key] = str(dest)
        return manifest

    return {
        key: str(src)
        for key, (_, src) in output_sources.items()
    }


def _open_scratch(dest: Path) -> "tuple[Path, int | None]":
    """Create this publish's scratch file and hold it locked while it is written.

    The scratch name is unique per call, which is what makes removing it on
    failure safe — but only for a failure the process lives through. A worker
    killed mid-copy (SIGKILL from the container runtime, the watchdog's
    ``os._exit`` hard exit on an in-process wedge) runs no cleanup at all, and
    leaves a ``.part`` file in ``temp/<task_id>`` that outlives it. Nothing
    could then tell that file apart from a *live* attempt's scratch file, so
    nothing dared remove it: it sat in the staging dir keeping the backend
    consumer's non-recursive ``rmdir`` from ever succeeding, one leaked
    directory (plus a partial GB-scale artifact) per killed publish.

    An exclusive ``flock`` held for the lifetime of the scratch file is the
    difference, because the kernel drops it when the process dies however it
    dies. A lock that can still be taken therefore means no live attempt is
    writing that file, which is exactly what :func:`_sweep_dead_scratch` needs
    to reclaim a predecessor's leftovers without touching a running one's.

    Returns the scratch path and the locked descriptor, or ``None`` for the
    descriptor when the platform or filesystem has no ``flock`` (Windows;
    exotic mounts). Unlocked is the pre-existing behaviour and stays safe:
    the sweep refuses to reclaim what it cannot prove is dead, so those
    scratch files are simply never swept.
    """
    # Fixed-length name: an output filename has no length limit of its own,
    # so building the scratch name out of it could overrun NAME_MAX.
    part = dest.with_name(f".{uuid.uuid4().hex}.part")
    for _ in range(2):
        # O_EXCL so this is provably a name nothing else holds, and created
        # *before* the copy opens it: the gap it closes is the copy's own
        # first write, which is where a multi-GB publish spends its time and
        # so where a kill lands.
        fd = os.open(part, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
        if fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                pass  # filesystem without flock — leave it unlocked
            else:
                if os.fstat(fd).st_nlink:
                    return part, fd
                # A concurrent attempt's sweep unlinked this name in the
                # window between the create and the lock, so the lock now
                # protects an inode no name points at. Take a fresh name —
                # once, since losing twice means losing the same microsecond
                # window on both tries; the copy creates whatever it is
                # handed, and an unlocked scratch file is merely unsweepable.
                os.close(fd)
                part = dest.with_name(f".{uuid.uuid4().hex}.part")
                continue
        os.close(fd)
        break
    return part, None


def _sweep_dead_scratch(dest_dir: Path) -> None:
    """Remove scratch files left by attempts that were killed mid-publish.

    Called once per local publish, before this attempt stages anything, so a
    retry of a task whose predecessor was killed mid-copy starts from a
    staging dir holding published outputs and nothing else — the layout
    ``docs/fleet/conventions.md`` § 10 promises consumers, whose sweep is a
    non-recursive ``rmdir`` that any leftover defeats.

    Only a file whose lock this can take is removed: a live attempt of the
    same task holds an exclusive ``flock`` on its scratch file for as long as
    it is writing it (see :func:`_open_scratch`), and the kernel releases it
    when — and only when — that process dies. So this reclaims a dead
    predecessor's residue and leaves a running successor's alone, which is
    the distinction that made blanket reclamation unsafe. Anything it cannot
    prove is dead it leaves in place, including every scratch file on a
    filesystem without ``flock``: an orphan an operator sweeps beats
    destroying a live publish.

    Published outputs are never candidates — only the exact
    ``.<uuid4 hex>.part`` shape is, and only inside this task's own staging
    dir. Best-effort throughout: a publish must not fail over housekeeping.
    """
    if fcntl is None:
        return
    try:
        names = os.listdir(dest_dir)
    except OSError:
        return
    swept = []
    for name in names:
        if not _SCRATCH_NAME_RE.fullmatch(name):
            continue
        path = dest_dir / name
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            continue  # a live attempt is writing it, or the fs has no locks
        else:
            try:
                os.unlink(path)
                swept.append(name)
            except OSError:
                pass
        finally:
            os.close(fd)
    if swept:
        log.warning(
            "reclaimed %d abandoned scratch file(s) (%s) under %s: a previous "
            "attempt of this task was killed mid-publish. Its partial output "
            "is gone; this attempt republishes from scratch.",
            len(swept), ", ".join(swept), dest_dir,
        )


async def _stage_output(
    src: Path,
    dest: Path,
    *,
    cancelled: Optional[asyncio.Event] = None,
    cancel_message: str,
) -> "tuple[int, int]":
    """Publish one output to its staged path; return the published identity.

    Copy to a scratch name in the same directory, then ``os.replace`` it onto
    ``dest``. Never straight into ``dest``: every attempt of a task stages
    into the same ``temp/<task_id>``, and a copy *truncates the destination
    in place*, so an attempt landing on a path a concurrent attempt had
    already published rewrote that attempt's file underneath it — both
    completing, one of them pointing its manifest at a half-overwritten
    artifact. The rename makes the published name change atomically from one
    complete file to another, so concurrent attempts can only be last-writer
    wins, never interleaved. It costs nothing: same directory, so it is a
    metadata operation, not a second copy — which is why it runs inline on
    the event loop rather than in a thread that a cancel could not stop.

    The scratch name is this call's alone, which is what makes removing it on
    failure safe — it is the only name in the staging dir no other attempt
    can hold. It is also held under an exclusive ``flock`` for as long as it
    exists, so the one failure this call cannot clean up after — being killed
    outright, mid-copy — leaves a scratch file the *next* attempt can prove
    dead and reclaim; see :func:`_open_scratch` and
    :func:`_sweep_dead_scratch`. The published name is never removed; see the
    module docstring.

    The returned ``(st_dev, st_ino)`` is that same scratch file's, read before
    the rename that moves it onto ``dest`` — so it identifies the artifact
    *this* call published, and only ever that one. It is what
    :func:`_unowned_outputs` re-checks ``dest`` against later, so an attempt
    whose published name a concurrent attempt has since taken over can find
    out before it reports a manifest pointing at the other attempt's bytes.
    """
    try:
        part, lock_fd = _open_scratch(dest)
    except FileNotFoundError:
        # The staging dir is the backend consumer's to sweep, and it
        # ``rmdir``s it the moment it comes up empty — which is exactly what
        # it is between our caller's mkdir and this scratch file's creation.
        # Losing that race would fail an attempt whose outputs are fine. The
        # dir being gone is the whole problem and mkdir makes it again, so
        # remaking it is the whole repair: one retry, because losing twice
        # means losing the same sub-millisecond window on both tries. Only
        # the create can lose it: from here on the dir holds this attempt's
        # scratch file, so a non-recursive rmdir cannot empty it — which is
        # why the copy below now treats ``FileNotFoundError`` as the *source*
        # being missing, and leaves that to the caller as before.
        dest.parent.mkdir(parents=True, exist_ok=True)
        part, lock_fd = _open_scratch(dest)
    try:
        await _copyfile_async(
            src, part, cancelled=cancelled, cancel_message=cancel_message,
        )
        # Read from the scratch name, never from ``dest`` after the rename:
        # ``dest`` is shared with every attempt of this task, so a successor's
        # publish landing in the interval would be adopted as this attempt's —
        # the exact misidentification the check exists to catch. ``os.replace``
        # preserves the inode, so the scratch file's identity *is* the
        # published artifact's.
        ident = os.stat(part)
        # Synchronous on purpose — the one filesystem call on this path that
        # does not go through ``asyncio.to_thread``. ``to_thread`` is not
        # cancellable: cancelling the await abandons the thread, which goes
        # on to perform the rename anyway. So a task cancel landing there
        # published the artifact *after* ``_stage_output`` raised —
        # ``upload_outputs`` never recorded it in ``staged``, leaving a
        # GB-scale file on the shared volume that the orphan warning could
        # not name, while the cleanup below unlinked ``part`` out from under
        # the rename still using it. Run inline there is no suspension point
        # between the rename and the caller's ``staged.append``, so a
        # publish is either not done or done and recorded. Affordable
        # because ``part`` and ``dest`` share a directory: this is a
        # metadata operation, not the copy, which stays off the loop above.
        os.replace(part, dest)
        return ident.st_dev, ident.st_ino
    except BaseException:
        # ``_copyfile_async`` already removes a destination it opened; this is
        # for the paths it doesn't cover (a failed ``os.replace``, a cancel
        # between the two). Scratch files are the one thing in the staging
        # dir no consumer will ever pick up, so leaving one is worse than
        # useless — it keeps the dir non-empty and defeats the backend's
        # rmdir. Non-raising: a real exception is already propagating — and
        # synchronous for the same reason as the rename, since an ``await``
        # while unwinding a cancel is a second cancellation point, and one
        # that fires here strands the scratch file for good.
        try:
            os.unlink(part)
        except OSError:
            pass
        raise
    finally:
        # Releases the scratch file's lock. Past the rename there is nothing
        # left to protect: the inode is published under a name no sweep looks
        # at, and this attempt owns it until a successor renames over it.
        if lock_fd is not None:
            os.close(lock_fd)


def _unowned_outputs(
    owned: "dict[str, tuple[int, int]] | None",
) -> "list[str]":
    """Return the published paths that are no longer this attempt's artifact.

    ``owned`` is what :func:`upload_outputs` recorded: the ``(st_dev, st_ino)``
    each staged path had when this attempt's rename committed it. Every attempt
    of a task publishes into the same ``temp/<task_id>``, so a concurrent
    attempt — one the backend handed the task to after a watchdog ``fail`` or a
    stale-task sweep, while this attempt was still running — can rename its own
    artifact onto those same names. The publish stays atomic, but the path in
    this attempt's manifest then holds *another attempt's* file: a complete,
    valid artifact that its result does not describe. Consumers move that path
    to the case's permanent home on a ``complete``, so nothing downstream ever
    finds out.

    A path is this attempt's only while the inode behind it is the one the
    rename put there. Missing counts as lost: this attempt is not terminal yet,
    so no consumer should have swept it, and an unreadable staging path is one
    this attempt cannot vouch for either.

    Non-raising like :func:`_warn_published_orphans`, and for the same reason —
    it runs on the path that owes the backend a terminal report.
    """
    lost: list[str] = []
    for path, ident in (owned or {}).items():
        try:
            st = os.stat(path)
        except OSError:
            lost.append(path)
            continue
        if (st.st_dev, st.st_ino) != ident:
            lost.append(path)
    return lost


def _warn_published_orphans(
    task: ClaimedTask,
    shared_volume_path: "str | None",
    published: "list[str] | None",
    *,
    reason: str,
) -> None:
    """Log the artifacts a failing attempt published and is leaving behind.

    Publishing succeeding is not the same as the task succeeding. A cancel
    that lands between the last upload and the ``CancelGuard`` exit, a
    watchdog deadline that fires while the manifest is being assembled, or a
    result the wire can't encode all leave a task fully published *and*
    reported failed — as does a publish that fails partway through. The
    backend only sweeps a staging dir (or accepts an output manifest) for a
    task it recorded as **complete**, so those artifacts — GB-scale for
    colmap-splat PLYs and Neural-Canvas splats — are orphans nothing ever
    reaches.

    This does not remove them, in either mode. Remote mode has no delete
    route in the worker protocol; local mode *could* unlink, but
    ``temp/<task_id>`` is shared by every attempt of the task and a failing
    attempt cannot prove the file at a published name is still its own — the
    backend may already have re-queued the task (the watchdog's synchronous
    ``fail`` from its own thread, or the stale-task sweeper with no report at
    all) and a successor may already have published there. A stat-then-unlink
    ownership check does not close that: POSIX has no compare-and-unlink, so
    the successor can land in the gap and lose a complete artifact. Safe
    reclamation is therefore deferred until the fleet has a staging path
    unique per attempt that the backend consumers understand, or a lease on
    the shared one. Until then this logs one WARNING an operator can
    reconcile from, which beats deleting a live attempt's outputs.

    Best-effort and non-raising by contract, and synchronous: it runs while a
    failure is already being reported, on the path that owes the backend a
    terminal status, so it must never displace the real failure or kill the
    polling loop — and with no filesystem work left to do there is nothing to
    push off the event loop.
    """
    try:
        if not published:
            return
        if (task.params or {}).get("input_files"):
            log.warning(
                "task %s: %d output file(s) (%s) remain published on the "
                "backend — %s, and the worker protocol has no delete route; "
                "a retry of this task will overwrite them.",
                task.id, len(published), ", ".join(published), reason,
            )
            return
        if not shared_volume_path:
            # Outputs never left the per-task workdir, which
            # ``Worker._run_one`` removes after every attempt.
            return
        log.warning(
            "task %s: %d staged output file(s) (%s) remain under %s — %s, so "
            "the backend will never sweep them. Left in place: the staging "
            "path is shared with any re-queued attempt of this task, so the "
            "SDK cannot prove they are still this attempt's to remove.",
            task.id, len(published), ", ".join(published),
            Path(shared_volume_path) / "temp" / str(task.id), reason,
        )
    except Exception as e:  # noqa: BLE001 — must not mask the real failure
        log.warning(
            "task %s: could not report published outputs: %s", task.id, e,
        )
