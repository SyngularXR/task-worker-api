"""Params schema for MODEL_INITIALIZING tasks.

Matches the shape produced by
services/backend/src/utils/extra_model_registry.py:_build_task_params.
"""
from __future__ import annotations

from pydantic import Field, field_validator

from ..conventions import preview_filename
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
    preview_max_triangles: int = Field(default=300_000, ge=300_000, le=300_000)
    remove_interior: bool = False
    yup: bool = True

    @field_validator("base_name")
    @classmethod
    def published_filename_is_safe(cls, value: str) -> str:
        if not value:
            return value

        from ..files import _require_safe_filename
        from ..errors import ProtocolError

        filename = preview_filename(value)
        try:
            _require_safe_filename(filename, field="base_name", key=filename)
        except ProtocolError as exc:
            raise ValueError("base_name must produce a safe published filename") from exc
        return value
