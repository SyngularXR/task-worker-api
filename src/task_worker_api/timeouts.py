"""Resolve per-task execution timeouts from constructor config + environment.

Pure functions — no I/O — so resolution is trivially testable. Resolution
order for a task type, highest priority first:

  env per-type  →  constructor per-type  →  env default  →  constructor default

A resolved value <= 0 means "no timeout" (escape hatch for a known-unbounded
task type).
"""
from __future__ import annotations

import logging
import math
from typing import Optional

from .enums import TaskType

log = logging.getLogger(__name__)

DEFAULT_TASK_TIMEOUT_S = 1800.0  # 30 minutes

_KNOWN_KEYS = frozenset(t.value for t in TaskType) | {"default"}


def _positive_finite_s(name: str, value: float) -> float:
    """Validate an SDK pacing interval, or raise ``ValueError``.

    These intervals pace SDK background work, and every degenerate value
    breaks that pacing in a way that is silent at construction and expensive in
    production — against the one backend the whole fleet shares:

    * ``poll_interval_s`` / ``claim_backoff_max_s``: ``0``/negative spins the
      claim loop at full speed, ``inf`` makes the first idle wait never end
      (the worker stops polling for good), and ``NaN`` fails every comparison
      in ``_claim_wait_s`` — so the cap never matches, the doubling loop runs
      once per accumulated failure, and ``asyncio.wait_for(timeout=nan)``
      waits forever.
    * ``heartbeat_interval_s``: ``<= 0`` makes every report's ``wait_for``
      time out immediately, so no heartbeat lands. Non-finite values break
      heartbeat timing: ``NaN`` is invalid and ``inf`` removes the report
      deadline and prevents subsequent ticks.
    * ``cancel_poll_interval_s`` (``CancelGuard.poll_interval_s``): ``<= 0``
      polls ``GET /tasks/{id}/cancel-status`` in a busy loop; non-finite
      values break the sleep or prevent subsequent polls.
    * ``timeout_grace_s``: ``NaN`` makes ``TaskWatchdog._wait`` return
      instantly at both grace phases (``end = now + nan``, so the loop never
      runs), collapsing SIGTERM → grace → SIGKILL → grace → hard-exit into an
      immediate container kill on the first deadline; ``<= 0`` does the same,
      and ``inf`` means the escalation never reaches the hard exit.

    Rejecting them here is what makes the capped-and-still-polling guarantee
    real.
    """
    if not math.isfinite(value) or value <= 0:
        raise ValueError(
            f"{name} must be a finite positive number of seconds "
            f"(got {value!r})"
        )
    return float(value)


def parse_timeouts_env(raw: Optional[str]) -> dict[str, float]:
    """Parse ``WORKER_TASK_TIMEOUTS='default=1800,gs_build=7200'`` into a dict.

    Keys are lowercased task-type values plus the literal ``default``. Malformed
    and non-finite entries are skipped with a WARNING; this never raises (a bad
    env var must not crash worker startup). Non-finite values are rejected
    because they defeat the timeout silently: ``nan`` fails ``_run_one``'s
    ``timeout_s > 0`` check so the watchdog is never started, and ``inf``
    starts one whose deadline never arrives. A key that is neither a task type
    nor ``default`` is kept but logged at WARNING, since nothing ever reads it.
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
            value = float(val.strip())
        except ValueError:
            log.warning(
                "WORKER_TASK_TIMEOUTS: ignoring non-numeric value in %r", part
            )
            continue
        if not math.isfinite(value):
            log.warning(
                "WORKER_TASK_TIMEOUTS: ignoring non-finite value in %r", part
            )
            continue
        if key not in _KNOWN_KEYS:
            log.warning(
                "WORKER_TASK_TIMEOUTS: unknown key %r in %r has no effect "
                "(not a task type or 'default')", key, part
            )
        result[key] = value
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
