"""Resolve per-task execution timeouts from constructor config + environment.

Pure functions — no I/O — so resolution is trivially testable. Resolution
order for a task type, highest priority first:

  env per-type  →  constructor per-type  →  env default  →  constructor default

A resolved value <= 0 means "no timeout" (escape hatch for a known-unbounded
task type).
"""
from __future__ import annotations

import logging
from typing import Optional

from .enums import TaskType

log = logging.getLogger(__name__)

DEFAULT_TASK_TIMEOUT_S = 1800.0  # 30 minutes


def parse_timeouts_env(raw: Optional[str]) -> dict[str, float]:
    """Parse ``WORKER_TASK_TIMEOUTS='default=1800,gs_build=7200'`` into a dict.

    Keys are lowercased task-type values plus the literal ``default``. Malformed
    entries are skipped with a WARNING; this never raises (a bad env var must
    not crash worker startup).
    """
    result: dict[str, float] = {}
    if not raw:
        return result
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            log.warning(
                "WORKER_TASK_TIMEOUTS: ignoring malformed entry %r (no '=')", part
            )
            continue
        key, _, val = part.partition("=")
        key = key.strip().lower()
        try:
            result[key] = float(val.strip())
        except ValueError:
            log.warning(
                "WORKER_TASK_TIMEOUTS: ignoring non-numeric value in %r", part
            )
    return result


def resolve_task_timeout(
    task_type: TaskType,
    *,
    default_s: float,
    per_type: dict[TaskType, float],
    env: dict[str, float],
) -> float:
    """Resolve the effective timeout (seconds) for ``task_type``. <=0 = disabled."""
    tv = task_type.value
    if tv in env:
        return env[tv]
    if task_type in per_type:
        return per_type[task_type]
    if "default" in env:
        return env["default"]
    return default_s
