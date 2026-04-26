"""Per-worker JSONL capture of every claimed task envelope.

See docs/superpowers/specs/2026-04-26-payload-logging-design.md for the
full design rationale (two streams, per-process files for scaled replicas,
mtime-based retention, never-raises contract).
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, TextIO

from .context import ClaimedTask

log = logging.getLogger(__name__)

_WINDOWS_RESERVED = (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def sanitize_worker_id(worker_id: str) -> str:
    """Make a worker_id safe to use as a path segment on Linux and Windows."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", worker_id)
    base_for_check = cleaned.split(".", 1)[0].upper()
    if (
        not cleaned
        or cleaned in {".", ".."}
        or base_for_check in _WINDOWS_RESERVED
    ):
        cleaned = cleaned + "_x"
    return cleaned


class PayloadLogger:
    """Append claimed-task envelopes to per-worker JSONL files.

    Owned by Worker; never instantiated directly by SDK consumers. The
    failure contract is broad: every public method must return without
    raising, even on disk-full / permission / serialization errors.
    """

    def __init__(
        self,
        *,
        root: Path,
        worker_id: str,
        retention_days: int = 14,
        enabled: bool = True,
        _now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        _pid: Callable[[], int] = os.getpid,
        _boot_id: Optional[str] = None,
    ) -> None:
        self.root = root
        self.worker_id = worker_id
        self.retention_days = retention_days
        self.enabled = enabled
        self._now = _now
        self._pid = _pid
        self.boot_id = _boot_id or uuid.uuid4().hex[:8]
        # per-stream {date, handle} state — keyed by file-stem ("payloads"
        # or "raw_envelopes"), not the JSON `stream` label.
        self._handles: dict[str, tuple[str, TextIO]] = {}
        self._warned_once = False
        if self.enabled:
            self.root.mkdir(parents=True, exist_ok=True)

    # ----- public API ---------------------------------------------------

    def record(self, task: ClaimedTask) -> None:
        """Append one typed-stream JSON line. Never raises. No-op when disabled."""
        if not self.enabled:
            return
        try:
            record = {
                "captured_at": self._now().isoformat(),
                "stream": "typed",
                "task_id": task.id,
                "task_type": task.task_type.value,
                "case_id": task.case_id,
                "item_key": task.item_key,
                "status": int(task.status),
                "params": task.params,
                "worker_id": task.worker_id or self.worker_id,
                "process_id": self._pid(),
                "boot_id": self.boot_id,
            }
            self._write_line("payloads", record)
        except Exception as exc:  # noqa: BLE001 — never-raises contract
            self._warn_once(exc)

    # ----- internals ----------------------------------------------------

    def _file_path(self, stream: str, date_str: str) -> Path:
        return self.root / (
            f"{stream}-{date_str}-pid{self._pid()}-{self.boot_id}.jsonl"
        )

    def _ensure_handle(self, stream: str) -> TextIO:
        date_str = self._now().date().isoformat()
        cached = self._handles.get(stream)
        if cached is None or cached[0] != date_str:
            if cached is not None:
                try:
                    cached[1].close()
                except Exception:  # noqa: BLE001
                    pass
            handle = open(self._file_path(stream, date_str), "a", encoding="utf-8")
            self._handles[stream] = (date_str, handle)
            return handle
        return cached[1]

    def _write_line(self, stream: str, record: dict) -> None:
        line = json.dumps(record, default=str)
        handle = self._ensure_handle(stream)
        handle.write(line + "\n")
        handle.flush()

    def _warn_once(self, exc: BaseException) -> None:
        if self._warned_once:
            return
        self._warned_once = True
        log.warning(
            "payload_log: I/O failure (further failures suppressed): %s: %s",
            type(exc).__name__, exc,
        )
