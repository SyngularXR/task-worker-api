"""Params schema for CINEMATIC_BAKING tasks.

Matches the shape produced by
services/backend/src/utils/extra_model_registry.py:_build_cb_params.
"""
from __future__ import annotations

from pydantic import Field, model_validator

from ._base import TaskParamsBase


class CinematicBakingParams(TaskParamsBase):
    """Input for the Blender worker's cinematic_baking handler."""

    job_id: str = Field(..., description="Stable job identifier for metadata mirror.")
    input_path: str = Field(..., description="Absolute preview GLB path on the shared volume.")
    base_name: str = Field(..., description="Filename stem for outputs; _finalized.glb appended.")
    input_files: dict[str, str] | None = Field(
        default=None,
        description=(
            "Remote-worker inputs: {filename: filename}, served via "
            "GET /tasks/{id}/files/{filename}. Emitted alongside input_path "
            "when the producing box enables cross-box files; home workers "
            "keep the zero-copy input_path, foreign workers use only this."
        ),
    )
    material_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]*$",
        description=(
            "Optional worker material registry id. Omit for the deployment's "
            "Current/default material."
        ),
    )
    pattern_scale: float | None = Field(
        default=None,
        ge=0.1,
        le=8.0,
        allow_inf_nan=False,
        description=(
            "Optional Bioform Pattern Scale override; requires material_id and "
            "must be supported by that material."
        ),
    )

    @model_validator(mode="after")
    def validate_material_options(self) -> "CinematicBakingParams":
        if self.pattern_scale is not None and self.material_id is None:
            raise ValueError("pattern_scale requires material_id")
        return self
