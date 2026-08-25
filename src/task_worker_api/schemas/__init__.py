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
from .generate_synthetic import GenerateSyntheticParams
from .gs_build import GsBuildParams, Gs4dBuildParams
from .model_initializing import ModelInitializingParams
from .segmentation import SegmentationParams
from .spatial_reconstruction import SpatialReconstructionParams

# render + apple_ml_gs land in a future release once the handler shapes
# are audited (see design spec Appendix A).
TASK_PARAMS_SCHEMAS: dict[TaskType, type[TaskParamsBase]] = {
    TaskType.DETECT_CUT_PLANES: DetectCutPlanesParams,
    TaskType.MODEL_INITIALIZING: ModelInitializingParams,
    TaskType.CINEMATIC_BAKING: CinematicBakingParams,
    TaskType.GS_BUILD: GsBuildParams,
    TaskType.GS4D_BUILD: Gs4dBuildParams,
    TaskType.SEGMENTATION: SegmentationParams,
    TaskType.DEPLOY_CASE: DeployCaseParams,
    TaskType.GENERATE_SYNTHETIC: GenerateSyntheticParams,
    TaskType.SPATIAL_RECONSTRUCTION: SpatialReconstructionParams,
    # FINALIZE_SYNTHETIC is backend-local only — no worker-side params schema,
    # and deliberately absent so the public create endpoint can't accept it.
}


__all__ = [
    "TaskParamsBase",
    "TASK_PARAMS_SCHEMAS",
    "CinematicBakingParams",
    "DeployCaseParams",
    "DetectCutPlanesParams",
    "GenerateSyntheticParams",
    "ModelInitializingParams",
    "GsBuildParams",
    "Gs4dBuildParams",
    "SegmentationParams",
    "SpatialReconstructionParams",
]
