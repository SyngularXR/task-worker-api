"""Params schema for GENERATE_SYNTHETIC tasks.

Synthetic multi-plane MRI super-resolution reconstruction (NiftyMIC),
run by the Neural-Canvas worker via the self-contained ``synthetic-generator``
image. All filesystem paths are **server-minted** by the SynPusher producer
(``generate_mri_fusion_candidate``) — this schema is intentionally NOT exposed
on the public ``POST /tasks/{task_type}`` endpoint (the backend denies it), so a
client cannot inject ``nifti_path`` / ``output_dir`` to drive arbitrary worker
file access. See
``docs/superpowers/specs/2026-06-15-generate-synthetic-worker-migration-design.md``.
"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field

from ._base import TaskParamsBase


class SyntheticSource(BaseModel):
    """One source MRI plane resolved server-side from a Dicom row."""

    model_config = ConfigDict(extra="forbid")

    dicom_id: int
    nifti_path: str = Field(..., description="Source NIfTI on the shared volume.")
    orientation: str = Field(..., description="axial | coronal | sagittal.")
    sequence_key: Optional[str] = Field(default=None)
    name: str


class GenerateSyntheticParams(TaskParamsBase):
    """Input for the synthetic-generator SRR compute."""

    sources: List[SyntheticSource] = Field(..., min_length=2, max_length=3)
    # Kept alongside ``sources`` because candidate suppression and the local
    # fallback handler still read ``source_dicom_ids``.
    source_dicom_ids: List[int]
    reference_dicom_id: int
    reference_size: List[int] = Field(..., description="Reference NIfTI GetSize() [X,Y,Z].")
    synthetic_roi: dict = Field(..., description="{'mode':'full'} or {'mode':'roi','bounds':{...}}.")
    staging_dir: str = Field(..., description="Server-minted staging dir on the shared volume.")
    output_dir: str = Field(..., description="Server-minted final dicom dir on the shared volume.")
    output_filename: str
    name: str
    author_id: int
    niftymic: dict = Field(default_factory=dict, description="NiftyMIC tunables; image defaults apply when empty.")
