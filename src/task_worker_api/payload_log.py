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
import time
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

# Two-stage size cap: cap individual variable-size fields at PAYLOAD_FIELD_CAP_BYTES
# (224KB) so we never serialize a 50MB params blob even once, and cap the final
# constructed record at RECORD_CAP_BYTES (256KB) so pathological non-payload fields
# (huge worker_id, huge error string in raw stream) can't sneak past either.
PAYLOAD_FIELD_CAP_BYTES = 224 * 1024
RECORD_CAP_BYTES = 256 * 1024


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
            try:
                self.root.mkdir(parents=True, exist_ok=True)
            except Exception as exc:  # noqa: BLE001 — never-raises includes __init__
                log.warning(
                    "payload_log: mkdir failed (%s: %s); disabling capture",
                    type(exc).__name__, exc,
                )
                self.enabled = False

    # ----- public API ---------------------------------------------------

    def record(self, task: ClaimedTask) -> None:
        """Append one typed-stream JSON line. Never raises. No-op when disabled."""
        if not self.enabled:
            return
        try:
            now = self._now()
            safe_params, truncated_size = self._maybe_truncate_field(task.params)
            if truncated_size:
                self._warn_truncated_once(task.id, "pre-serialization", truncated_size)
            record = {
                "captured_at": now.isoformat(),
                "stream": "typed",
                "task_id": task.id,
                "task_type": task.task_type.value,
                "case_id": task.case_id,
                "item_key": task.item_key,
                "status": int(task.status),
                "params": safe_params,
                "worker_id": task.worker_id or self.worker_id,
                "process_id": self._pid(),
                "boot_id": self.boot_id,
            }
            self._write_line("payloads", record, now=now)
        except Exception as exc:  # noqa: BLE001 — never-raises contract
            self._warn_once(exc)

    def record_raw(
        self,
        raw: Any,
        *,
        error_type: str,
        error: str,
    ) -> None:
        """Append one raw-envelope JSON line. Never raises. No-op when disabled.

        Called from BackendClient.claim_next when ClaimedTask.from_dict() raises
        OR when the response itself fails to parse as JSON. ``raw`` may be any
        JSON-coercible value (dict, list, None) or unparseable text.
        """
        if not self.enabled:
            return
        try:
            now = self._now()
            safe_raw, truncated_size = self._maybe_truncate_field(raw)
            if truncated_size:
                self._warn_truncated_once(None, "pre-serialization", truncated_size)
            record = {
                "captured_at": now.isoformat(),
                "stream": "raw",
                "raw": safe_raw,
                "error_type": error_type,
                "error": error,
                "worker_id": self.worker_id,
                "process_id": self._pid(),
                "boot_id": self.boot_id,
            }
            self._write_line("raw_envelopes", record, now=now)
        except Exception as exc:  # noqa: BLE001
            self._warn_once(exc)

    def cleanup_old_files(self) -> None:
        """Delete files past retention. Runs even when disabled. Never raises.

        Runs on three schedules — at Worker startup, on UTC date rollover, and
        on a periodic timer (configurable via WORKER_PAYLOAD_LOG_CLEANUP_INTERVAL_S).
        Disabled-mode still runs cleanup so a kill-switch deployment doesn't leave
        residual logs forever.
        """
        try:
            if not self.root.exists():
                return
            cutoff_seconds = self.retention_days * 86400
            now_ts = time.time()
            for entry in self.root.iterdir():
                if not entry.is_file():
                    continue
                if not (
                    entry.name.startswith("payloads-")
                    or entry.name.startswith("raw_envelopes-")
                ):
                    continue
                if not entry.name.endswith(".jsonl"):
                    continue
                try:
                    age = now_ts - entry.stat().st_mtime
                except OSError:
                    continue
                if age >= cutoff_seconds:
                    try:
                        entry.unlink()
                    except (FileNotFoundError, PermissionError, OSError):
                        # Race with another replica or a Windows handle still
                        # open elsewhere. Skip and continue.
                        continue
        except Exception as exc:  # noqa: BLE001
            self._warn_once(exc)

    def close(self) -> None:
        """Flush + close any open handles. Idempotent. Never raises."""
        try:
            for _date_str, handle in list(self._handles.values()):
                try:
                    handle.flush()
                    handle.close()
                except Exception:  # noqa: BLE001
                    continue
            self._handles.clear()
        except Exception as exc:  # noqa: BLE001
            self._warn_once(exc)

    # ----- internals ----------------------------------------------------

    def _file_path(self, stream: str, date_str: str) -> Path:
        return self.root / (
            f"{stream}-{date_str}-pid{self._pid()}-{self.boot_id}.jsonl"
        )

    def _ensure_handle(self, stream: str, now: datetime) -> TextIO:
        """Return the open handle for ``stream``, rotating on UTC date change.

        The caller passes the timestamp it already captured for ``captured_at``
        so we only call ``self._now()`` once per record() — keeps tests with
        injected iterator-based ``_now`` simple and avoids any window where
        two timestamps in one record could disagree about which day it is.
        """
        date_str = now.date().isoformat()
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

    def _write_line(self, stream: str, record: dict, *, now: datetime) -> None:
        line = self._serialize_record(record)
        handle = self._ensure_handle(stream, now=now)
        handle.write(line + "\n")
        handle.flush()

    def _maybe_truncate_field(self, value: Any) -> tuple[Any, Optional[int]]:
        """Return (value-or-marker, original_size_bytes_if_truncated_else_None).

        Serializes ``value`` exactly once. If the JSON form exceeds
        PAYLOAD_FIELD_CAP_BYTES, returns a small marker dict and the
        original byte count; otherwise returns the value unchanged.
        """
        serialized = json.dumps(value, default=str)
        size = len(serialized.encode("utf-8"))
        if size > PAYLOAD_FIELD_CAP_BYTES:
            return {"_truncated": True, "_original_size_bytes": size}, size
        return value, None

    def _serialize_record(self, record: dict) -> str:
        """Stage 2 of the size cap. The variable-size field has already been
        truncated by _maybe_truncate_field; this catches pathological wrappers
        (huge item_key, huge error string in raw stream)."""
        line = json.dumps(record, default=str)
        if len(line.encode("utf-8")) <= RECORD_CAP_BYTES:
            return line
        original_size = len(line.encode("utf-8"))
        self._warn_truncated_once(
            record.get("task_id"), "post-construction", original_size
        )
        replacement = {
            "_record_truncated": True,
            "_original_size_bytes": original_size,
            "task_id": record.get("task_id"),
            "stream": record.get("stream"),
            "captured_at": record.get("captured_at"),
        }
        return json.dumps(replacement, default=str)

    def _warn_truncated_once(
        self, task_id: Any, stage: str, size: int,
    ) -> None:
        if getattr(self, "_truncate_warned", False):
            return
        self._truncate_warned = True
        log.warning(
            "payload_log: truncated record (task_id=%s, stage=%s, size=%d bytes); "
            "further truncations suppressed",
            task_id, stage, size,
        )

    def _warn_once(self, exc: BaseException) -> None:
        if self._warned_once:
            return
        self._warned_once = True
        log.warning(
            "payload_log: I/O failure (further failures suppressed): %s: %s",
            type(exc).__name__, exc,
        )
