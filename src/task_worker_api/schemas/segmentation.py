"""Params schema for SEGMENTATION tasks.

Handled by Neural-Canvas's ``segment_unified`` pipeline when invoked as
a task worker. Covers the inputs needed to run a VISTA3D / MedSAM3
inference on a shared-volume NIfTI.
"""
from __future__ import annotations

from typing import Optional

from pydantic import Field

from ._base import TaskParamsBase


class SegmentationParams(TaskParamsBase):
    """Input for Neural-Canvas's segmentation handler."""

    input_path: str = Field(..., description="NIfTI volume on the shared volume.")
    model: str = Field(..., description="Segmentation model: 'vista3d' or 'medsam3'.")
    labels: list[str] = Field(default_factory=list, description="Target label names.")
    case_id: Optional[int] = Field(default=None)
    dicom_id: Optional[int] = Field(default=None)
    mask_id: Optional[str] = Field(default=None, description="Mask identifier for result mirror.")
