"""Params schema for DETECT_CUT_PLANES tasks.

Mirrored in Blender-CLI/src/blender_worker/features/detect_cut_planes.py
which reads these fields (with defaults) from task.params.
"""
from __future__ import annotations

from pydantic import Field

from ._base import TaskParamsBase


class DetectCutPlanesParams(TaskParamsBase):
    """Input for the Blender worker's detect_cut_planes handler."""

    input_path: str = Field(
        ...,
        description="Absolute path to the input STL on the shared volume.",
    )
    max_results: int = Field(default=10, ge=1, le=100)
    angle_tol_deg: float = Field(default=5.0, gt=0, le=90)
    planar_tol_mm: float = Field(default=0.3, gt=0)
    min_area_mm2: float = Field(default=20.0, ge=0)
    connected: bool = True
    connected_count: int = Field(default=3, ge=1)
