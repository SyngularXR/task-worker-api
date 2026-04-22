"""Params schema for GS_BUILD tasks.

Handled by the colmap-splat worker. Keys map to ``run.sh`` CLI flags in
``src/worker/handlers/gs_build.py``; most are optional because run.sh's
own defaults are the single source of truth for tuning.
"""
from __future__ import annotations

from typing import Optional

from pydantic import Field

from ._base import TaskParamsBase


class GsBuildParams(TaskParamsBase):
    """Input for the colmap-splat worker's gs_build handler."""

    # Scene location — absolute path under the shared volume, or relative
    # to ``SHARED_VOLUME_PATH``. `scene_path` is the historical alias.
    scene: Optional[str] = Field(default=None, description="Scene directory on shared volume.")
    scene_path: Optional[str] = Field(default=None, description="Alias for `scene`.")
    scene_id: Optional[str] = Field(default=None, description="Scene id; defaults to dir basename.")

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
