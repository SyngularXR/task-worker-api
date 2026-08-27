"""Verify the TASK_PARAMS_SCHEMAS registry covers every worker-facing TaskType."""
import pytest

from task_worker_api.enums import TaskType
from task_worker_api.schemas import (
    TASK_PARAMS_SCHEMAS,
    CinematicBakingParams,
    DeployCaseParams,
    GsBuildParams,
    Gs4dBuildParams,
)


def test_cinematic_baking_registered():
    assert TASK_PARAMS_SCHEMAS[TaskType.CINEMATIC_BAKING] is CinematicBakingParams


def test_cinematic_baking_roundtrip():
    schema = TASK_PARAMS_SCHEMAS[TaskType.CINEMATIC_BAKING]
    obj = schema(job_id="job1", input_path="/shared/preview.glb", base_name="skull")
    d = obj.model_dump()
    assert d == {
        "job_id": "job1",
        "input_path": "/shared/preview.glb",
        "base_name": "skull",
        "input_files": None,
        "material_id": None,
        "pattern_scale": None,
    }


def test_cinematic_baking_accepts_biomaterial_options():
    obj = CinematicBakingParams(
        job_id="job1",
        input_path="/shared/preview.glb",
        base_name="liver",
        material_id="liver",
        pattern_scale=1.25,
    )
    assert obj.material_id == "liver"
    assert obj.pattern_scale == 1.25


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), 0.09, 8.01])
def test_cinematic_baking_rejects_invalid_pattern_scale(value):
    with pytest.raises(Exception):
        CinematicBakingParams(
            job_id="j",
            input_path="/p",
            base_name="b",
            material_id="liver",
            pattern_scale=value,
        )


def test_cinematic_baking_pattern_scale_requires_material():
    with pytest.raises(Exception, match="pattern_scale requires material_id"):
        CinematicBakingParams(
            job_id="j", input_path="/p", base_name="b", pattern_scale=1.0
        )


@pytest.mark.parametrize(
    "material_id", ["", "Liver", "liver-red", " liver", "a" * 65]
)
def test_cinematic_baking_rejects_invalid_material_id(material_id):
    with pytest.raises(Exception):
        CinematicBakingParams(
            job_id="j", input_path="/p", base_name="b", material_id=material_id
        )


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


def test_gs_build_accepts_dense_init():
    obj = GsBuildParams(dense_init=True)
    assert obj.dense_init is True


def test_gs_build_dense_init_optional():
    obj = GsBuildParams()
    assert obj.dense_init is None


def test_gs_build_accepts_warm_start_ply():
    # 4D warm-chain: each phase after the first seeds from the prior phase's PLY.
    obj = GsBuildParams(scene="/shared/p1/gs", warm_start_ply="/shared/p0/gs/gs_output/gs_p0.ply")
    assert obj.warm_start_ply == "/shared/p0/gs/gs_output/gs_p0.ply"


def test_gs_build_warm_start_ply_optional():
    assert GsBuildParams().warm_start_ply is None


def test_gs4d_build_registered():
    """GS4D_BUILD must be in the registry so Worker.__init__ doesn't reject a
    handler for it (the colmap-splat worker reuses gs_build.run for 4D phases)."""
    assert TaskType.GS4D_BUILD in TASK_PARAMS_SCHEMAS


def test_gs4d_build_alias_shares_gs_build_model():
    """Gs4dBuildParams is an alias for GsBuildParams — same class object, so
    the 4D warm-chain reuses the identical params surface (scene, warm_start_ply,
    tuning knobs). A future divergence is a one-line class split."""
    assert Gs4dBuildParams is GsBuildParams
    assert TASK_PARAMS_SCHEMAS[TaskType.GS4D_BUILD] is GsBuildParams


def test_gs4d_build_accepts_warm_start_ply():
    obj = Gs4dBuildParams(scene="/shared/p1/gs", warm_start_ply="/shared/p0/gs/gs_output/gs_p0.ply")
    assert obj.warm_start_ply == "/shared/p0/gs/gs_output/gs_p0.ply"
