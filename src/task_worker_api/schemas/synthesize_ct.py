"""User-requested CT synthesis from one published prepared MRI."""
from uuid import UUID
from pydantic import PositiveInt
from ._base import TaskParamsBase


class SynthesizeCtParams(TaskParamsBase):
    input_path: str
    dicom_id: PositiveInt
    job_id: UUID
