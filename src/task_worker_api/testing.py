"""Test fixtures — ``FakeBackendClient`` drop-in for ``BackendClient``.

Lets worker tests exercise the full claim → handler → complete loop
without a real backend. Scripts a queue of tasks, captures completion +
failure payloads, and fakes the cancel-status poll.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, TYPE_CHECKING

from .context import ClaimedTask
from .enums import TaskStatus, TaskType
from .errors import TaskCancelled

if TYPE_CHECKING:  # pragma: no cover
    import asyncio


class FakeBackendClient:
    """Drop-in for ``BackendClient``. Keeps claim/complete/fail/progress
    payloads in memory; workers interact with it the same way they'd talk
    to the real backend."""

    def __init__(self) -> None:
        self._queue: list[ClaimedTask] = []
        self._next_id = 1
        self.completed_tasks: list[dict] = []
        self.failed_tasks: list[dict] = []
        self.progress_events: list[dict] = []
        self.cancelled_task_ids: set[int] = set()
        # Virtual file store for remote-mode tests. Inputs staged via
        # ``queue_file`` are keyed by (task_id, filename); downloads read
        # from here. Uploads are captured in ``uploaded_files`` so test
        # suites can assert on produced artifacts without mocking HTTP.
        self._files: dict[tuple[int, str], bytes] = {}
        self.uploaded_files: dict[tuple[int, str], bytes] = {}

    # ----- test-side API ------------------------------------------

    def queue_task(
        self,
        *,
        task_type: TaskType,
        params: dict,
        case_id: Optional[int] = None,
        item_key: str = "",
    ) -> ClaimedTask:
        task = ClaimedTask(
            id=self._next_id,
            task_type=task_type,
            case_id=case_id,
            item_key=item_key,
            status=TaskStatus.PENDING,
            params=params,
            worker_id=None,
        )
        self._next_id += 1
        self._queue.append(task)
        return task

    def mark_cancelled(self, task_id: int) -> None:
        """Simulate a user cancel — next cancel poll will report cancelled."""
        self.cancelled_task_ids.add(task_id)

    def queue_file(self, task_id: int, filename: str, content: "bytes | str") -> None:
        """Stage a virtual input file for ``download_file`` to serve.

        Remote-mode tests call this to seed the inputs a handler expects,
        instead of mocking HTTP. ``content`` may be ``str`` (encoded utf-8)
        or raw ``bytes``. A download for an unstaged (task_id, filename)
        raises ``FileNotFoundError``, mirroring a 404 from the real backend.
        """
        if isinstance(content, str):
            content = content.encode("utf-8")
        self._files[(task_id, filename)] = content

    # ----- BackendClient protocol ---------------------------------

    async def claim_next(self, task_types, worker_id: str) -> Optional[ClaimedTask]:
        allowed = set(
            t.value if hasattr(t, "value") else str(t) for t in task_types
        )
        for i, t in enumerate(self._queue):
            if t.task_type.value in allowed:
                return self._queue.pop(i)
        return None

    async def report_progress(
        self, task_id: int, *, stage: str, current: int = 0, total: int = 0,
        kill_handle=None,
    ) -> dict:
        self.progress_events.append({
            "task_id": task_id, "stage": stage,
            "current": current, "total": total,
        })
        return {"cancelled": task_id in self.cancelled_task_ids}

    async def get_cancel_status(self, task_id: int) -> dict:
        return {
            "cancelled": task_id in self.cancelled_task_ids,
            "status": int(
                TaskStatus.CANCELLED if task_id in self.cancelled_task_ids
                else TaskStatus.IN_PROGRESS
            ),
            "cancelled_reason": "user" if task_id in self.cancelled_task_ids else None,
        }

    async def poll_cancel_status(self, task_id: int) -> dict:
        """One-shot cancel-poll — mirrors ``BackendClient.poll_cancel_status``.

        The fake doesn't simulate HTTP retries, so this delegates to
        :meth:`get_cancel_status` (same data). Tests that need to assert
        on the one-shot (no-retry) contract use ``httpx.MockTransport``
        against the real ``BackendClient`` (see ``test_client_retry.py``).
        """
        return await self.get_cancel_status(task_id)

    async def complete(self, task_id: int, result: dict) -> None:
        self.completed_tasks.append({"task_id": task_id, "result": result})

    async def fail(self, task_id: int, error: str) -> None:
        self.failed_tasks.append({"task_id": task_id, "error": error})

    async def download_file(
        self, task_id: int, filename: str, dest: Path,
        *,
        cancelled: "Optional[asyncio.Event]" = None,
    ) -> None:
        """Serve a file previously staged via :meth:`queue_file`.

        Writes the staged bytes to ``dest`` (matching the real backend's
        stream-to-disk contract). Raises ``FileNotFoundError`` for an
        unstaged (task_id, filename) — the in-memory analogue of a 404.

        A set ``cancelled`` event raises ``TaskCancelled`` and writes
        nothing, mirroring ``BackendClient.download_file``, which checks the
        event before the request and before each chunk write. The fake has no
        chunks to interleave, so the pre-request check is the whole contract
        here — enough for worker-level tests to see a mid-staging cancel
        abort the download rather than land a file on disk.
        """
        if cancelled is not None and cancelled.is_set():
            raise TaskCancelled(
                f"task {task_id} cancelled by user while downloading {filename}"
            )
        key = (task_id, filename)
        if key not in self._files:
            raise FileNotFoundError(
                f"FakeBackendClient has no staged file for task {task_id} "
                f"named {filename!r} — call queue_file() to seed it."
            )
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self._files[key])

    async def upload_file(
        self, task_id: int, filename: str, src: Path,
        *,
        cancelled: "Optional[asyncio.Event]" = None,
    ) -> None:
        """Capture an uploaded artifact in :attr:`uploaded_files`.

        Reads the source file bytes (as the real backend would receive
        them) and stores them keyed by (task_id, filename) so test suites
        can assert on produced outputs without a live HTTP endpoint.

        A set ``cancelled`` event raises ``TaskCancelled`` and uploads
        nothing, mirroring ``BackendClient.upload_file``, which checks the
        event before the request and aborts the request if it fires
        mid-stream. The fake has no stream to interleave, so the pre-request
        check is the whole contract here — enough for worker-level tests to
        see a mid-publish cancel abort the upload rather than land a file on
        the backend.
        """
        if cancelled is not None and cancelled.is_set():
            raise TaskCancelled(
                f"task {task_id} cancelled by user while uploading {filename}"
            )
        self.uploaded_files[(task_id, filename)] = src.read_bytes()

    async def close(self) -> None:
        pass

    async def __aenter__(self) -> "FakeBackendClient":
        return self

    async def __aexit__(self, *_exc) -> None:
        pass
