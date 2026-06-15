"""Params schema for SEGMENTATION tasks.

Handled by Neural-Canvas's ``segment_unified`` pipeline when invoked as a task
worker. Covers the full one-shot request surface (MedSAM3 / VISTA3D / OpenMAP /
TotalSegmentator / one-shot MedSAM2), mirroring nexus-core's ``SegmentRequest``
and Neural-Canvas's ``UnifiedSegmentRequest``.

Field families:
- ``input_path`` / ``output_dir`` — shared-volume paths. ``output_dir`` is a
  server-minted durable directory the worker writes the mask into (it must
  survive the SDK's post-/complete work-dir cleanup), so nexus-core's finalize
  worker can import it.
- ``result_mask_*`` / ``case_id`` / ``dicom_id`` / ``mask_id`` / ``result_frame``
  — backend routing/import context consumed by nexus-core's finalize worker.
  The Neural-Canvas handler ignores these except ``output_dir`` and
  ``label_prompt`` / ``labels``.
- model-specific knobs — only the fields relevant to ``model`` are forwarded by
  the handler into ``UnifiedSegmentRequest``.
"""
from __future__ import annotations

from typing import Optional

from pydantic import Field

from ._base import TaskParamsBase


class SegmentationParams(TaskParamsBase):
    """Input for Neural-Canvas's one-shot segmentation handler."""

    input_path: str = Field(..., description="NIfTI volume on the shared volume.")
    model: str = Field(
        ...,
        description="vista3d | medsam3 | medsam2 | openmap_* | totalsegmentator_*",
    )
    output_dir: str = Field(
        ...,
        description="Durable shared dir the worker writes the mask into (server-minted).",
    )

    # Backend routing / import context (Neural-Canvas ignores these).
    result_mask_id: Optional[str] = Field(default=None)
    result_mask_name: Optional[str] = Field(default=None)
    result_mask_color: Optional[str] = Field(default=None)
    case_id: Optional[int] = Field(default=None)
    dicom_id: Optional[int] = Field(default=None)
    mask_id: Optional[str] = Field(default=None, description="Applied user mask id.")
    result_frame: Optional[str] = Field(default=None, description="'primary' | 'native'.")

    # Shared knobs.
    min_volume_cm3: float = 0.0
    boundary_smoothing: bool = True
    boundary_smoothing_strength: str = "balanced"
    bbox: Optional[list[int]] = None

    # MedSAM3.
    prompt: Optional[str] = None
    axis: int = 0
    threshold: float = 0.5
    nms_iou: float = 0.5
    window_center: Optional[float] = None
    window_width: Optional[float] = None
    volume_threshold: float = 0.1
    merge_regions: bool = False
    fill_empty_slices: bool = False
    max_fill_gap: int = 0

    # VISTA3D.
    target: Optional[str] = None
    modality: Optional[str] = None
    label_prompt: Optional[list[int]] = None
    intensity_refine: bool = False
    grow_max_distance: int = 0
    grow_shrink_voxels: int = 0
    grow_gradient_threshold: float = 0.0
    grow_intensity_sigma: float = 0.0
    grow_debug: bool = False
    smooth_edge: bool = False
    smooth_seeds: bool = False

    # OpenMAP.
    output_ext: str = ".nii.gz"

    # TotalSegmentator.
    fast: bool = False

    # One-shot MedSAM2 (box-seed; NOT the interactive warm session).
    seed_slice: Optional[int] = None
    seed_box: Optional[list[int]] = None
    medsam2_modality: Optional[str] = None

    # DEPRECATED: alias for label_prompt, kept one release for a half-rolled
    # fleet (design A1). Remove in 0.10.0. The handler prefers label_prompt.
    labels: list[str] = Field(default_factory=list)
