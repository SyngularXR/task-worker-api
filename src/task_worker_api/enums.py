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
    # 4D Gaussian Splatting build — one orchestrator over N cardiac phases with a
    # per-frame state machine + warm-start (4D cardiac design map, sub-project #4).
    GS4D_BUILD = "gs4d_build"
    SEGMENTATION = "segmentation"
    MODEL_INITIALIZING = "model_initializing"
    APPLE_ML_GS = "apple_ml_gs"
    DETECT_CUT_PLANES = "detect_cut_planes"
    CINEMATIC_BAKING = "cinematic_baking"
    DEPLOY_CASE = "deploy_case"
    # Synthetic MRI super-resolution reconstruction (NiftyMIC). GENERATE is the
    # remote SRR compute (Neural-Canvas worker → synthetic-generator image);
    # FINALIZE is the backend-local DB import step (never claimed by a worker).
    GENERATE_SYNTHETIC = "generate_synthetic"
    FINALIZE_SYNTHETIC = "finalize_synthetic"
    # One-shot segmentation finalize: backend-local DB/manifest import step for a
    # completed SEGMENTATION task (never claimed by a worker; absent from
    # TASK_PARAMS_SCHEMAS), mirroring FINALIZE_SYNTHETIC. Value kept <=20 chars to
    # fit the backend's task_type column (CharEnumField max_length=20).
    FINALIZE_SEGMENTATION = "finalize_segment"
    SPATIAL_RECONSTRUCTION = "spatial_recon"
    SPATIAL_GS_BUILD = "spatial_gs_build"


class TaskStatus(IntEnum):
    """Lifecycle states for a task. Int values match the DB column."""

    PENDING = 0
    CLAIMED = 1
    IN_PROGRESS = 2
    COMPLETED = 3
    FAILED = 4
    CANCELLED = 5
