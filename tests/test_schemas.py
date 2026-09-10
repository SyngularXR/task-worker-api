"""Verify the TASK_PARAMS_SCHEMAS registry covers every worker-facing TaskType."""
import pytest
from pydantic import ValidationError

from task_worker_api.enums import TaskType
from task_worker_api.schemas import (
    TASK_PARAMS_SCHEMAS,
    CinematicBakingParams,
    DeployCaseParams,
    PrepareDeployParams,
    GsBuildParams,
    Gs4dBuildParams,
)


def test_prepare_deploy_requires_the_staged_recipe():
    assert TASK_PARAMS_SCHEMAS[TaskType.PREPARE_DEPLOY] is PrepareDeployParams
    assert PrepareDeployParams().model_dump() == {"recipe_path": "recipe.json"}
    for raw in ({"recipe_path": "../recipe.json"}, {"recipe_path": "/live/recipe.json"}, {"content_path": "/live"}):
        with pytest.raises(ValidationError):
            PrepareDeployParams(**raw)


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
        "yup": True,
        "max_displacement_mm": None,
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
    with pytest.raises(Exception, match="pattern_scale requires a Bioform material_id"):
        CinematicBakingParams(
            job_id="j", input_path="/p", base_name="b", pattern_scale=1.0
        )


@pytest.mark.parametrize("material,value", [(None, 0.5), ("current", 0.5),
    ("liver", -0.1), ("liver", 5.01), ("liver", float("nan")), ("liver", float("inf"))])
def test_cinematic_displacement_rejects_invalid_options(material, value):
    with pytest.raises(ValidationError):
        CinematicBakingParams(job_id="j", input_path="/p", base_name="b",
                             material_id=material, max_displacement_mm=value)


def test_cinematic_displacement_and_axis_roundtrip():
    params = CinematicBakingParams(job_id="j", input_path="/p", base_name="b",
                                  material_id="liver", max_displacement_mm=0.5, yup=False)
    assert CinematicBakingParams.model_validate_json(params.model_dump_json()) == params
    assert params.max_displacement_mm == 0.5 and params.yup is False


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
    assert obj.model_dump(exclude_unset=True) == {"content_path": "/app/shared/content/abc123", "build_target": "iOS"}


def test_deploy_snapshot_contract_roundtrip():
    raw = dict(snapshot_path="/shared/deploy/case/1234abcd", output_path="/shared/assetbundle/case/1234abcd",
               case_guid="case", deploy_hash="1234abcd", build_target="Android", platform="android", schema_version=1)
    params = DeployCaseParams(**raw)
    assert params.model_dump(exclude_unset=True) == raw
    assert DeployCaseParams.model_validate_json(params.model_dump_json()) == params
    with pytest.raises(ValidationError):
        DeployCaseParams(**{**raw, "schema_version": 0})


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


def test_gs_build_accepts_cross_box_scene_bundle():
    params = GsBuildParams(
        scene="/app/shared/grid/gs",
        input_path="/app/shared/grid/gs/train_images/000.png",
        input_files={"scene": "scene.zip"},
    )
    assert params.input_files == {"scene": "scene.zip"}


def test_gs4d_build_registered():
    assert TASK_PARAMS_SCHEMAS[TaskType.GS4D_BUILD] is Gs4dBuildParams


def test_gs4d_build_stages_match_backend_queue():
    assert Gs4dBuildParams(stage="render").n_cameras == 0
    assert Gs4dBuildParams(stage="finalize", n_phases=3).n_phases == 3


def test_gs4d_build_rejects_training_params():
    with pytest.raises(ValidationError):
        Gs4dBuildParams(stage="render", warm_start_ply="seed.ply")
