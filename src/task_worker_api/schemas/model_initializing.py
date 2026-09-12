"""Params schema for MODEL_INITIALIZING tasks.

Matches the shape produced by
services/backend/src/utils/extra_model_registry.py:_build_task_params.
"""
from __future__ import annotations

from pydantic import Field

from ._base import TaskParamsBase


class ModelInitializingParams(TaskParamsBase):
    """Input for the Blender worker's model_initializing handler."""

    job_id: str = Field(..., description="Stable job identifier for metadata mirror.")
    input_path: str = Field(..., description="Absolute STL path on the shared volume.")
    base_name: str = Field(..., description="Filename stem for outputs.")
    input_files: dict[str, str] | None = Field(
        default=None,
        description=(
            "Remote-worker inputs: {filename: filename}, served via "
            "GET /tasks/{id}/files/{filename}. Emitted alongside input_path "
            "when the producing box enables cross-box files; home workers "
            "keep the zero-copy input_path, foreign workers use only this."
        ),
    )
    # Same Blender options and ceilings as the existing mesh-preprocessing CLI.
    # Admission validates these before importing the compute handler.
    convex_hull_target_faces: int = Field(default=500, ge=1, le=2_000_000)
    convex_hull_margin: float = Field(default=1.6, ge=0.0, le=100.0, allow_inf_nan=False)
    convex_hull_smooth_iterations: int = Field(default=3, ge=0)
    preview_max_triangles: int = Field(default=120_000, ge=1, le=120_000)
    remove_interior: bool = False
    yup: bool = True
