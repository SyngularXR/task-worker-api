"""Spatial Gaussian build task contract."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from task_worker_api.enums import TaskType
from task_worker_api.schemas import TASK_PARAMS_SCHEMAS, SpatialGsBuildParams


def _payload() -> dict[str, object]:
    capture_hash = "a88091c7a88091c7a88091c7a88091c7"
    return {
        "case_guid": "CASE-GUID",
        "capture_hash": capture_hash,
        "capture_root_rel": f"case_data/spatial_capture/{capture_hash}",
        "coordinate_frame": "syngar_anchor_v1",
        "iterations": 5_000,
        "max_image_size": 1_008,
        "max_splats": 200_000,
    }


def test_spatial_gs_build_schema_registered_and_roundtrips():
    assert TaskType.SPATIAL_GS_BUILD.value == "spatial_gs_build"
    assert TASK_PARAMS_SCHEMAS[TaskType.SPATIAL_GS_BUILD] is SpatialGsBuildParams
    assert SpatialGsBuildParams.model_validate(_payload()).iterations == 5_000


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("capture_root_rel", "case_data/spatial_capture/other"),
        ("coordinate_frame", "opencv_world"),
        ("iterations", -1),
        ("max_image_size", 0),
        ("max_splats", 0),
    ],
)
def test_spatial_gs_build_rejects_invalid_contract(field: str, value: object):
    payload = _payload()
    payload[field] = value
    with pytest.raises(ValidationError):
        SpatialGsBuildParams.model_validate(payload)


def test_spatial_gs_build_does_not_accept_arbitrary_scene_path():
    with pytest.raises(ValidationError):
        SpatialGsBuildParams.model_validate({**_payload(), "scene": "/tmp/escape"})
