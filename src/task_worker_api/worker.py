"""The Worker class — claim → run handler → complete.

Drops ~500 lines of boilerplate from every worker repo. A handler is just
``async def run(ctx: TaskContext, params: TypedParamsModel) -> dict``;
the Worker class owns claim, stage-inputs, heartbeat, cancel guard,
publish-outputs, error handling, and the polling loop.

Two modes of use:

1. Pure worker — ``asyncio.run(Worker(...).run_forever())``.
2. Hybrid — ``await run_hybrid(uvicorn.Server.serve(), worker)`` when the
   process also runs a FastAPI app (Neural-Canvas pattern).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
import traceback
import urllib.request
from pathlib import Path
from typing import Awaitable, Callable, Optional

from .cancel import CancelGuard
from .client import BackendClient
from .context import ClaimedTask, TaskContext
from .enums import TaskType
from .errors import ProtocolError, TaskCancelled, TaskParamsError
from .files import prepare_inputs, upload_outputs
from .payload_log import PayloadLogger, sanitize_worker_id
from .progress import ProgressReporter
from .schemas import TASK_PARAMS_SCHEMAS, TaskParamsBase
from .timeouts import DEFAULT_TASK_TIMEOUT_S, parse_timeouts_env, resolve_task_timeout
from .watchdog import TaskWatchdog, TerminalGuard, list_descendants

log = logging.getLogger(__name__)

HandlerFn = Callable[[TaskContext, TaskParamsBase], Awaitable[dict]]


def _make_sync_fail(
    base_url: str, api_key: str, task_id: int, *, timeout_s: float = 3.0,
):
    """Build a synchronous ``fail(error)`` callable for the watchdog thread.

    The async BackendClient is bound to the (possibly blocked) event loop, so
    the watchdog's last-resort report uses plain stdlib urllib with an explicit
    short timeout — it must never become a second wedge. Matches the wire
    format of ``BackendClient.fail``: PUT /tasks/{id}/fail {"error": ...}.
    """
    url = f"{base_url.rstrip('/')}/tasks/{task_id}/fail"

    def _sync_fail(error: str) -> None:
        data = json.dumps({"error": error}).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="PUT",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        urllib.request.urlopen(req, timeout=timeout_s).close()

    return _sync_fail


class Worker:
    """Glues everything together. One instance per worker process.

    Construction:
        worker = Worker(
            backend_url="http://backend:5000/api/v1",
            api_key=os.environ["WORKER_API_KEY"],
            worker_id="blender-worker-1",
            handlers={
                TaskType.DETECT_CUT_PLANES: detect_cut_planes.run,
                TaskType.MODEL_INITIALIZING: model_initializing.run,
            },
        )
        await worker.run_forever()
    """

    def __init__(
        self,
        *,
        backend_url: str,
        api_key: str,
        worker_id: str,
        handlers: dict[TaskType, HandlerFn],
        work_dir: Optional[str] = None,
        shared_volume_path: Optional[str] = None,
        poll_interval_s: float = 5.0,
        heartbeat_interval_s: float = 10.0,
        cancel_poll_interval_s: float = 2.0,
        request_timeout_s: float = 30.0,
        task_timeout_s: float = DEFAULT_TASK_TIMEOUT_S,
        task_timeouts: Optional[dict] = None,
        timeout_grace_s: float = 15.0,
        on_hard_exit: Optional[Callable[[], None]] = None,
        client: Optional[BackendClient] = None,
        _watchdog_factory: Callable[..., object] = TaskWatchdog,
    ):
        self.backend_url = backend_url
        self.api_key = api_key
        self.worker_id = worker_id
        self.handlers = handlers
        self.work_dir = Path(
            work_dir or os.environ.get("WORKER_WORKDIR") or tempfile.gettempdir()
        )
        self.shared_volume_path = shared_volume_path
        self.poll_interval_s = poll_interval_s
        self.heartbeat_interval_s = heartbeat_interval_s
        self.cancel_poll_interval_s = cancel_poll_interval_s
        self.task_timeout_s = task_timeout_s
        self.task_timeouts = task_timeouts or {}
        self.timeout_grace_s = timeout_grace_s
        self._on_hard_exit = on_hard_exit or (lambda: os._exit(75))
        self._timeout_env = parse_timeouts_env(os.environ.get("WORKER_TASK_TIMEOUTS"))
        self._watchdog_factory = _watchdog_factory

        self._payload_logger = self._build_payload_logger()

        if client is None:
            self._client = BackendClient(
                backend_url, api_key, timeout_s=request_timeout_s,
                payload_logger=self._payload_logger,
            )
        else:
            # Externally-supplied client (e.g. FakeBackendClient in tests) is
            # used as-is; we don't reach in and rewrite its state.
            self._client = client
        self._stop = asyncio.Event()

        # Fail fast on misconfiguration: an empty handlers dict makes
        # task_types=[] in run_forever's poll loop, so the worker silently
        # polls forever without ever processing work. Operators only notice
        # via the absence of activity. Same shape of misconfiguration as
        # the missing-schema case below.
        if not self.handlers:
            raise ProtocolError(
                "handlers is empty; Worker has nothing to claim. "
                "Register at least one TaskType→handler mapping."
            )

        # Fail fast on misconfiguration: any handler whose TaskType has no
        # registered params schema would only blow up after claim_next pulls
        # the first matching task off the queue, burning retry budget. Same
        # check lives in _run_one as defense-in-depth for code paths that
        # mutate handlers post-construction.
        missing = [t.value for t in self.handlers if t not in TASK_PARAMS_SCHEMAS]
        if missing:
            raise ProtocolError(
                "no schema registered for task_type "
                f"{', '.join(sorted(missing))}; "
                "update task-worker-api or register one locally"
            )

    def _build_payload_logger(self) -> PayloadLogger:
        """Construct a PayloadLogger from env + shared_volume_path.

        When shared_volume_path is None the logger is disabled (no place to
        write). When the env var WORKER_PAYLOAD_LOG_ENABLED is "false" it's
        disabled regardless. Bad WORKER_PAYLOAD_LOG_RETENTION_DAYS values
        fall back to 14 days with a WARNING.
        """
        env_enabled = (
            os.environ.get("WORKER_PAYLOAD_LOG_ENABLED", "true").lower() != "false"
        )
        enabled = bool(self.shared_volume_path) and env_enabled

        retention_raw = os.environ.get("WORKER_PAYLOAD_LOG_RETENTION_DAYS", "14")
        try:
            retention = int(retention_raw)
            if retention < 1:
                raise ValueError(f"retention must be >= 1, got {retention}")
        except (ValueError, TypeError):
            log.warning(
                "payload_log: WORKER_PAYLOAD_LOG_RETENTION_DAYS=%r is invalid; "
                "falling back to 14 days",
                retention_raw,
            )
            retention = 14

        if self.shared_volume_path:
            root = (
                Path(self.shared_volume_path)
                / "_worker_payloads"
                / sanitize_worker_id(self.worker_id)
            )
        else:
            # Placeholder; logger is disabled so it's never touched.
            root = Path("/__payload_log_disabled__")

        return PayloadLogger(
            root=root,
            worker_id=self.worker_id,
            retention_days=retention,
            enabled=enabled,
        )

    @property
    def task_types(self) -> list[TaskType]:
        """Types this worker can claim, derived from registered handlers."""
        return list(self.handlers.keys())

    async def shutdown(self) -> None:
        """Ask the polling loop to exit after the current task finishes."""
        self._stop.set()

    async def run_forever(self) -> None:
        """Main polling loop. Returns when shutdown() is called."""
        self.work_dir.mkdir(parents=True, exist_ok=True)
        log.info(
            "task-worker-api Worker starting: id=%s url=%s types=%s",
            self.worker_id, self.backend_url,
            ",".join(t.value for t in self.task_types),
        )
        if self._payload_logger.enabled:
            log.info(
                "payload logging: enabled, root=%s, retention=%dd",
                self._payload_logger.root, self._payload_logger.retention_days,
            )
        else:
            log.info(
                "payload logging: disabled (shared_volume_path=%r, env=%r)",
                self.shared_volume_path,
                os.environ.get("WORKER_PAYLOAD_LOG_ENABLED", "true"),
            )
        self._payload_logger.cleanup_old_files()

        cleanup_interval_s = float(
            os.environ.get("WORKER_PAYLOAD_LOG_CLEANUP_INTERVAL_S", "3600")
        )
        cleanup_task = asyncio.create_task(
            self._periodic_cleanup_loop(cleanup_interval_s)
        )

        try:
            while not self._stop.is_set():
                claimed = await self._claim()
                if claimed is None:
                    try:
                        await asyncio.wait_for(
                            self._stop.wait(),
                            timeout=self.poll_interval_s,
                        )
                    except asyncio.TimeoutError:
                        pass
                    continue
                await self._run_one(claimed)
        finally:
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass
            self._payload_logger.close()
            await self._client.close()
            log.info("task-worker-api Worker stopped: id=%s", self.worker_id)

    async def _periodic_cleanup_loop(self, interval_s: float) -> None:
        """Background timer that re-runs cleanup so idle workers stay honest.

        Without this, a worker that wrote files 30 days ago and then sat
        dormant would never clean up — rollover-triggered cleanup only fires
        on the next write. The timer fires every interval_s seconds; when
        shutdown is requested, the loop returns immediately.
        """
        try:
            while True:
                await asyncio.sleep(interval_s)
                if self._stop.is_set():
                    return
                self._payload_logger.cleanup_old_files()
        except asyncio.CancelledError:
            raise

    async def run_one(self) -> bool:
        """Process exactly one claim cycle. Returns True iff a task ran.

        Test seam — production code uses ``run_forever``.
        """
        claimed = await self._claim()
        if claimed is None:
            return False
        await self._run_one(claimed)
        return True

    # ----- internals ----------------------------------------------

    async def _claim(self) -> Optional[ClaimedTask]:
        try:
            return await self._client.claim_next(
                self.task_types, worker_id=self.worker_id,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("claim failed against %s: %s", self.backend_url, e)
            return None

    async def _run_one(self, task: ClaimedTask) -> None:
        """Stage inputs → run handler under heartbeat + cancel guard → publish.

        A per-task watchdog (when ``timeout`` > 0) enforces a wall-clock
        deadline off the event loop. Terminal reporting goes through a single
        TerminalGuard so a timeout and a near-simultaneous normal completion
        report exactly once (first resolver wins).
        """
        task_dir = self.work_dir / f"task_{task.id}"
        progress = ProgressReporter(
            self._client, task.id,
            heartbeat_interval_s=self.heartbeat_interval_s,
        )

        timeout_s = resolve_task_timeout(
            task.task_type,
            default_s=self.task_timeout_s,
            per_type=self.task_timeouts,
            env=self._timeout_env,
        )
        guard = TerminalGuard()
        wd = None
        if timeout_s > 0:
            log.info(
                "task %s: %s timeout=%.0fs",
                task.id, task.task_type.value, timeout_s,
            )
            wd = self._watchdog_factory(
                timeout_s=timeout_s,
                grace_s=self.timeout_grace_s,
                guard=guard,
                sync_fail=_make_sync_fail(self.backend_url, self.api_key, task.id),
                on_hard_exit=self._on_hard_exit,
                children_before=list_descendants(os.getpid()),
            )
            wd.start()

        outcome: tuple[str, object] = ("fail", "unknown")
        try:
            # Capture BEFORE schema validation so malformed payloads — exactly
            # the bugs most worth replaying — still produce a typed-stream
            # record. record() never raises (see PayloadLogger contract).
            self._payload_logger.record(task)

            handler = self.handlers.get(task.task_type)
            if handler is None:
                raise ProtocolError(
                    f"no handler registered for task_type {task.task_type.value}"
                )

            params_schema = TASK_PARAMS_SCHEMAS.get(task.task_type)
            if params_schema is None:
                raise ProtocolError(
                    f"no schema registered for task_type {task.task_type.value}; "
                    "update task-worker-api or register one locally"
                )

            try:
                typed_params = params_schema(**task.params)
            except Exception as e:  # noqa: BLE001
                raise TaskParamsError(
                    f"task.params failed schema validation on claim: {e}"
                ) from e

            file_ctx = await prepare_inputs(task, self._client, task_dir)
            ctx = TaskContext(task=task, files=file_ctx, progress=progress)

            await progress.start_heartbeat()
            async with CancelGuard(
                self._client, task.id,
                poll_interval_s=self.cancel_poll_interval_s,
            ):
                result = await handler(ctx, typed_params)

            output_files = (result or {}).get("output_files") or {}
            if output_files:
                delivered = await upload_outputs(
                    task, self._client, file_ctx, output_files,
                    self.shared_volume_path,
                )
                result = {**result, "output_files": delivered}
            outcome = ("complete", result or {})

        except TaskCancelled:
            outcome = ("fail", "cancelled by user")
        except (TaskParamsError, ProtocolError) as e:
            log.error("task %s protocol error: %s", task.id, e)
            outcome = ("fail", str(e))
        except Exception as e:  # noqa: BLE001
            tb = traceback.format_exc()
            log.error("task %s failed: %s\n%s", task.id, e, tb)
            outcome = ("fail", f"{type(e).__name__}: {e}\n{tb}")
        finally:
            fired = wd.stop() if wd is not None else False
            await progress.stop()
            # Single terminal report. If the watchdog fired, the deadline won;
            # otherwise report the handler outcome. The guard makes this
            # exactly-once even against the watchdog's hard-exit path.
            if guard.claim():
                try:
                    if fired:
                        await self._client.fail(
                            task.id, f"timeout: exceeded {timeout_s:.0f}s",
                        )
                        log.warning(
                            "task %s timed out (%s)", task.id, task.task_type.value
                        )
                    elif outcome[0] == "complete":
                        await self._client.complete(task.id, outcome[1])
                        log.info(
                            "task %s completed (%s)", task.id, task.task_type.value
                        )
                    else:
                        await self._client.fail(task.id, outcome[1])
                        if outcome[1] == "cancelled by user":
                            log.info("task %s cancelled by user", task.id)
                except Exception:  # noqa: BLE001
                    pass
            shutil.rmtree(task_dir, ignore_errors=True)


async def run_hybrid(
    app_coro: Awaitable[None],
    worker: Worker,
) -> None:
    """Run an awaitable (e.g. uvicorn.Server.serve()) and a Worker concurrently.

    If either exits, the other is cancelled cleanly. Used by Neural-
    Canvas where the FastAPI server and the task worker share one
    process + event loop.

    Implemented with `asyncio.wait` + cancel (not `asyncio.TaskGroup`)
    to keep Python 3.10 compatibility. Neural-Canvas currently runs
    3.10 and can't easily jump to 3.11; raising the SDK's floor would
    strand that consumer.
    """
    app_task = asyncio.ensure_future(app_coro)
    worker_task = asyncio.ensure_future(worker.run_forever())
    try:
        done, pending = await asyncio.wait(
            {app_task, worker_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        # Cancel whichever sibling is still running so we don't leak.
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        # Re-raise the first exception from the completed side so the
        # caller sees it (TaskGroup behavior equivalent).
        for t in done:
            exc = t.exception()
            if exc is not None and not isinstance(exc, asyncio.CancelledError):
                raise exc
    finally:
        for t in (app_task, worker_task):
            if not t.done():
                t.cancel()
