"""Per-worker JSONL capture of every claimed task envelope.

See docs/superpowers/specs/2026-04-26-payload-logging-design.md for the
full design rationale (two streams, per-process files for scaled replicas,
mtime-based retention, never-raises contract).
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Optional

_WINDOWS_RESERVED = (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def sanitize_worker_id(worker_id: str) -> str:
    """Make a worker_id safe to use as a path segment on Linux and Windows.

    Replaces characters outside ``[A-Za-z0-9._-]`` with ``_`` (covers
    forward/back slashes, colons, spaces, etc.). Then appends ``_x`` if
    the result is empty, dot-only, or matches a Windows reserved device
    name (CON, PRN, AUX, NUL, COM1-9, LPT1-9, with or without an
    extension — Windows treats e.g. ``CON.log`` as the device too).

    The output is purely a path segment — no separators ever appear.
    """
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
        _boot_id: Optional[str] = None,
    ) -> None:
        self.root = root
        self.worker_id = worker_id
        self.retention_days = retention_days
        self.enabled = enabled
        self.boot_id = _boot_id or uuid.uuid4().hex[:8]
        if self.enabled:
            self.root.mkdir(parents=True, exist_ok=True)
