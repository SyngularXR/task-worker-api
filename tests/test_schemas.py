"""Verify the TASK_PARAMS_SCHEMAS registry covers every worker-facing TaskType."""
import pytest

from task_worker_api.enums import TaskType
from task_worker_api.schemas import TASK_PARAMS_SCHEMAS, CinematicBakingParams, DeployCaseParams


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


def test_deploy_case_registered():
    assert TASK_PARAMS_SCHEMAS[TaskType.DEPLOY_CASE] is DeployCaseParams


def test_deploy_case_roundtrip():
    obj = DeployCaseParams(content_path="/app/shared/content/abc123", build_target="iOS")
    assert obj.model_dump() == {"content_path": "/app/shared/content/abc123", "build_target": "iOS"}


def test_deploy_case_default_build_target():
    obj = DeployCaseParams(content_path="/app/shared/content/abc123")
    assert obj.build_target == "Android"


def test_deploy_case_rejects_extra_field():
    with pytest.raises(Exception):
        DeployCaseParams(content_path="/p", surprise="extra")


def test_deploy_case_content_path_required():
    with pytest.raises(Exception):
        DeployCaseParams()
