"""Typed `params` schemas, one per TaskType.

`TASK_PARAMS_SCHEMAS` maps each TaskType to its Pydantic model. Backend
validates incoming `POST /tasks/{task_type}` bodies against the matching
schema. Workers (in a future phase) re-validate on claim as
defense-in-depth.
"""
from __future__ import annotations

from ..enums import TaskType
from ._base import TaskParamsBase
from .cinematic_baking import CinematicBakingParams
from .deploy_case import DeployCaseParams
from .detect_cut_planes import DetectCutPlanesParams
from .gs_build import GsBuildParams
from .model_initializing import ModelInitializingParams
from .segmentation import SegmentationParams

# render + apple_ml_gs land in a future release once the handler shapes
# are audited (see design spec Appendix A).
TASK_PARAMS_SCHEMAS: dict[TaskType, type[TaskParamsBase]] = {
    TaskType.DETECT_CUT_PLANES: DetectCutPlanesParams,
    TaskType.MODEL_INITIALIZING: ModelInitializingParams,
    TaskType.CINEMATIC_BAKING: CinematicBakingParams,
    TaskType.GS_BUILD: GsBuildParams,
    TaskType.SEGMENTATION: SegmentationParams,
    TaskType.DEPLOY_CASE: DeployCaseParams,
}


__all__ = [
    "TaskParamsBase",
    "TASK_PARAMS_SCHEMAS",
    "CinematicBakingParams",
    "DeployCaseParams",
    "DetectCutPlanesParams",
    "ModelInitializingParams",
    "GsBuildParams",
    "SegmentationParams",
]
