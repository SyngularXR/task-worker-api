import pytest
from pydantic import ValidationError

from task_worker_api.schemas import DetectCutPlanesParams


@pytest.mark.parametrize("field", ("angle_tol_deg", "planar_tol_mm", "min_area_mm2"))
@pytest.mark.parametrize("value", (float("nan"), float("inf"), float("-inf")), ids=("nan", "inf", "-inf"))
def test_float_params_reject_non_finite_values(field, value):
    with pytest.raises(ValidationError):
        DetectCutPlanesParams(input_path="/p", **{field: value})


def test_defaults():
    params = DetectCutPlanesParams(input_path="/p")
    assert params.angle_tol_deg == 5.0
    assert params.planar_tol_mm == 0.3
    assert params.min_area_mm2 == 20.0


def test_json_round_trip():
    params = DetectCutPlanesParams(input_path="/p")
    assert DetectCutPlanesParams.model_validate_json(params.model_dump_json()) == params
