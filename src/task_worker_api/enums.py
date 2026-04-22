"""Canonical enums for the task system.

Mirrors the backend's `services.backend.src.database.task_models.TaskType`
and `.TaskStatus`. String values must match exactly; Phase 0.5 of the
spec verified that Tortoise `CharEnumField` accepts an externally-imported
Enum subclass, so the backend's DB column uses this class directly.
"""
from __future__ import annotations

from enum import Enum, IntEnum


class TaskType(str, Enum):
    """Recognised task types in the unified queue."""

    RENDER = "render"
    GS_BUILD = "gs_build"
    SEGMENTATION = "segmentation"
    MODEL_INITIALIZING = "model_initializing"
    APPLE_ML_GS = "apple_ml_gs"
    DETECT_CUT_PLANES = "detect_cut_planes"
    CINEMATIC_BAKING = "cinematic_baking"


class TaskStatus(IntEnum):
    """Lifecycle states for a task. Int values match the DB column."""

    PENDING = 0
    CLAIMED = 1
    IN_PROGRESS = 2
    COMPLETED = 3
    FAILED = 4
    CANCELLED = 5
