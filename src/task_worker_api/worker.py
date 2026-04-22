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
import logging
import os
import shutil
import tempfile
import traceback
from pathlib import Path
from typing import Awaitable, Callable, Optional

from .cancel import CancelGuard
from .client import BackendClient
from .context import ClaimedTask, TaskContext
from .enums import TaskType
from .errors import ProtocolError, TaskCancelled, TaskParamsError
from .files import prepare_inputs, upload_outputs
from .progress import ProgressReporter
from .schemas import TASK_PARAMS_SCHEMAS, TaskParamsBase

log = logging.getLogger(__name__)

HandlerFn = Callable[[TaskContext, TaskParamsBase], Awaitable[dict]]


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
        client: Optional[BackendClient] = None,
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

        self._client = client or BackendClient(
            backend_url, api_key, timeout_s=request_timeout_s,
        )
        self._stop = asyncio.Event()

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
            await self._client.close()
            log.info("task-worker-api Worker stopped: id=%s", self.worker_id)

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
        """Stage inputs → run handler under heartbeat + cancel guard → publish."""
        task_dir = self.work_dir / f"task_{task.id}"
        progress = ProgressReporter(
            self._client, task.id,
            heartbeat_interval_s=self.heartbeat_interval_s,
        )

        try:
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

            await self._client.complete(task.id, result or {})
            log.info("task %s completed (%s)", task.id, task.task_type.value)

        except TaskCancelled:
            log.info("task %s cancelled by user", task.id)
            try:
                await self._client.fail(task.id, "cancelled by user")
            except Exception:  # noqa: BLE001
                pass
        except (TaskParamsError, ProtocolError) as e:
            log.error("task %s protocol error: %s", task.id, e)
            try:
                await self._client.fail(task.id, str(e))
            except Exception:  # noqa: BLE001
                pass
        except Exception as e:  # noqa: BLE001
            tb = traceback.format_exc()
            log.error("task %s failed: %s\n%s", task.id, e, tb)
            try:
                await self._client.fail(
                    task.id, f"{type(e).__name__}: {e}\n{tb}",
                )
            except Exception:  # noqa: BLE001
                pass
        finally:
            await progress.stop()
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
