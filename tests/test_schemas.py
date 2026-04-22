"""Verify the TASK_PARAMS_SCHEMAS registry covers every worker-facing TaskType."""
import pytest

from task_worker_api.enums import TaskType
from task_worker_api.schemas import TASK_PARAMS_SCHEMAS, CinematicBakingParams


def test_cinematic_baking_registered():
    assert TASK_PARAMS_SCHEMAS[TaskType.CINEMATIC_BAKING] is CinematicBakingParams


def test_cinematic_baking_roundtrip():
    schema = TASK_PARAMS_SCHEMAS[TaskType.CINEMATIC_BAKING]
    obj = schema(job_id="job1", input_path="/shared/preview.glb", base_name="skull")
    d = obj.model_dump()
    assert d == {"job_id": "job1", "input_path": "/shared/preview.glb", "base_name": "skull"}


def test_cinematic_baking_rejects_extra_field():
    schema = TASK_PARAMS_SCHEMAS[TaskType.CINEMATIC_BAKING]
    with pytest.raises(Exception):
        schema(job_id="j", input_path="/p", base_name="b", surprise="extra")
