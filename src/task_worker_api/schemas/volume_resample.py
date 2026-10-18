"""Backend-owned scalar-volume resampling; enhancement is a separate operation."""
from typing import Literal
from uuid import UUID
from pydantic import Field, PositiveInt

from ._base import TaskParamsBase


class VolumeResampleParams(TaskParamsBase):
    input_path: str
    modality: Literal["CT", "CTA", "PET", "MRI"]
    method: Literal["ram2", "linear"]
    acquisition_plane: Literal["axial", "coronal", "sagittal"] = "axial"
    dicom_id: PositiveInt
    job_id: UUID
    phase_files: dict[str, str] = Field(default_factory=dict)
