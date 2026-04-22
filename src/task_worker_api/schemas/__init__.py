"""Typed `params` schemas, one per TaskType.

`TASK_PARAMS_SCHEMAS` maps each TaskType to its Pydantic model. Backend
validates incoming `POST /tasks/{task_type}` bodies against the matching
schema. Workers (in a future phase) re-validate on claim as
defense-in-depth.
"""
from __future__ import annotations

from ..enums import TaskType
from ._base import TaskParamsBase
from .detect_cut_planes import DetectCutPlanesParams
from .model_initializing import ModelInitializingParams

# NOTE: Not every TaskType has a schema yet. Render/gs_build/segmentation/
# apple_ml_gs land during Phase 1 proper; v0.1.0 ships the two Blender
# task types needed to unblock detect_cut_planes end-to-end and
# model_initializing (already in production via the existing worker).
#
# Unregistered task types can still be enqueued via the backend's
# per-feature endpoints (render_job.py, gs_build.py, intelligence.py)
# until their schemas land here.
TASK_PARAMS_SCHEMAS: dict[TaskType, type[TaskParamsBase]] = {
    TaskType.DETECT_CUT_PLANES: DetectCutPlanesParams,
    TaskType.MODEL_INITIALIZING: ModelInitializingParams,
}


__all__ = [
    "TaskParamsBase",
    "TASK_PARAMS_SCHEMAS",
    "DetectCutPlanesParams",
    "ModelInitializingParams",
]
