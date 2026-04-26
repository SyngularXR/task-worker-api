"""Per-worker JSONL capture of every claimed task envelope.

See docs/superpowers/specs/2026-04-26-payload-logging-design.md for the
full design rationale (two streams, per-process files for scaled replicas,
mtime-based retention, never-raises contract).
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Optional


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
