from uuid import uuid4
import pytest
from pydantic import ValidationError
from task_worker_api.enums import TaskType
from task_worker_api.schemas import SynthesizeCtParams, TASK_PARAMS_SCHEMAS


def test_ct_contract_is_source_owned_and_rejects_overrides():
    values = dict(input_path="input.nii.gz", dicom_id=3, job_id=uuid4())
    assert TASK_PARAMS_SCHEMAS[TaskType.SYNTHESIZE_CT] is SynthesizeCtParams
    assert SynthesizeCtParams(**values).dicom_id == 3
    for invalid in ({"model": "CBCT"}, {"dicom_id": 0}, {"job_id": "invalid"}):
        with pytest.raises(ValidationError):
            SynthesizeCtParams(**{**values, **invalid})
