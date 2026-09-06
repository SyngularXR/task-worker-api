"""Params schema for GS_BUILD tasks.

Handled by the colmap-splat worker. Keys map to ``run.sh`` CLI flags in
``src/worker/handlers/gs_build.py``; most are optional because run.sh's
own defaults are the single source of truth for tuning.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import Field, model_validator

from ._base import TaskParamsBase
from .spatial_reconstruction import SpatialReconstructionParams


class GsBuildParams(TaskParamsBase):
    """Input for the colmap-splat worker's gs_build handler."""

    # Scene location — absolute path under the shared volume, or relative
    # to ``SHARED_VOLUME_PATH``. `scene_path` is the historical alias.
    scene: Optional[str] = Field(default=None, description="Scene directory on shared volume.")
    scene_path: Optional[str] = Field(default=None, description="Alias for `scene`.")
    scene_id: Optional[str] = Field(default=None, description="Scene id; defaults to dir basename.")
    input_path: Optional[str] = Field(
        default=None,
        description="Home-box marker file; keeps local claims on the shared-volume path.",
    )
    input_files: Optional[dict[str, str]] = Field(
        default=None,
        description="Foreign-worker scene bundle, normally {'scene': 'scene.zip'}.",
    )

    # 4D warm-chain (sub-project #4): seed this phase's Gaussian init from a
    # prior phase's trained PLY (run.sh `--warm-start-ply`). Absolute path under
    # the shared volume. None → cold start (the first phase / all static builds).
    warm_start_ply: Optional[str] = Field(
        default=None,
        description="Prior-phase PLY to warm-start from (4D); absolute, on shared volume.",
    )

    # Tuning knobs — all optional, run.sh defaults apply when omitted.
    method: Optional[str] = Field(default=None)
    iterations: Optional[int] = Field(default=None, ge=0)
    max_image_size: Optional[int] = Field(default=None, ge=0)
    max_splats: Optional[int] = Field(default=None, ge=0)
    sh_degree: Optional[int] = Field(default=None, ge=0)
    seed: Optional[int] = Field(default=None, ge=0)
    sift_max_image_size: Optional[int] = Field(default=None, ge=0)
    num_threads: Optional[int] = Field(default=None, ge=0)
    background: Optional[str] = Field(default=None)
    strategy: Optional[str] = Field(default=None)
    dense_init: Optional[bool] = Field(
        default=None,
        description="Run COLMAP dense MVS for splat init (adds 5–30 min).",
    )


# 4D Gaussian Splatting (cardiac) builds reuse the same params surface as a
# static gs_build — the per-phase training tasks are dispatched as GS4D_BUILD
# (or GS_BUILD for the cold-start phase) and consume the identical field set,
# including `warm_start_ply` for the 4D warm-chain. The alias lets the registry
# and codegen surface a distinct name (Gs4dBuildParams) while sharing one
# Pydantic model, so a future divergence is a one-line class split rather than
# a cross-repo migration.
Gs4dBuildParams = GsBuildParams


class SpatialGsBuildParams(SpatialReconstructionParams):
    """Build a Gaussian Aura from one immutable spatial capture."""

    coordinate_frame: Literal["syngar_anchor_v1"]
    iterations: Optional[int] = Field(default=None, gt=0)
    max_image_size: Optional[int] = Field(default=None, gt=0)
    max_splats: Optional[int] = Field(default=None, gt=0)
    input_id: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    input_manifest_sha256: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def require_pinned_input_pair(self):
        if (self.input_id is None) != (self.input_manifest_sha256 is None):
            raise ValueError("input_id and input_manifest_sha256 must be supplied together")
        return self
