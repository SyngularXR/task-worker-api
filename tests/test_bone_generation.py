from uuid import uuid4
import pytest
from pydantic import ValidationError
from task_worker_api.enums import TaskType
from task_worker_api.schemas import BoneGenerationParams,TASK_PARAMS_SCHEMAS

def test_bone_generation_contract_requires_orientation():
 data=dict(input_path='input.nii.gz',dicom_id=1,job_id=uuid4(),orientation='RSP')
 assert TASK_PARAMS_SCHEMAS[TaskType.GENERATE_BONE] is BoneGenerationParams
 assert BoneGenerationParams(**data).orientation=='RSP'
 for changes in [{'orientation':'RRR'},{'orientation':'bad'},{'orientation':''},{'dicom_id':0},{'method':'linear'}]:
  with pytest.raises(ValidationError): BoneGenerationParams(**{**data,**changes})
