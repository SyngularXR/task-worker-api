"""Automatic source-paired T1 bone generation; publication is backend-owned."""
from uuid import UUID
from pydantic import Field, PositiveInt, field_validator
from ._base import TaskParamsBase


class BoneGenerationParams(TaskParamsBase):
    input_path: str
    dicom_id: PositiveInt
    job_id: UUID
    orientation: str = Field(pattern=r"^[RLAPSI]{3}$")

    @field_validator("orientation")
    @classmethod
    def scanner_axes(cls, value):
        if any(sum(letter in axis for letter in value) != 1 for axis in ("RL", "AP", "IS")):
            raise ValueError("Orientation must name each physical axis once")
        return value
