"""Spatial reconstruction task contract and shared coordinate fixture."""

from __future__ import annotations

import json
from importlib.resources import files

import pytest
from pydantic import ValidationError

from task_worker_api.enums import TaskType
from task_worker_api.schemas import TASK_PARAMS_SCHEMAS, SpatialReconstructionParams


def _valid_payload() -> dict[str, str]:
    return {
        "case_guid": "CASE-GUID",
        "capture_hash": "a88091c7a88091c7a88091c7a88091c7",
        "capture_root_rel": "case_data/spatial_capture/a88091c7a88091c7a88091c7a88091c7",
        "coordinate_frame": "syngar_anchor_v1",
    }


def _matrix(values: list[float]) -> list[list[float]]:
    return [values[index : index + 4] for index in range(0, 16, 4)]


def _multiply(left: list[list[float]], right: list[list[float]]) -> list[list[float]]:
    return [
        [sum(left[row][k] * right[k][column] for k in range(4)) for column in range(4)]
        for row in range(4)
    ]


def _transform(matrix: list[list[float]], point: list[float]) -> list[float]:
    return [
        sum(matrix[row][k] * [*point, 1.0][k] for k in range(4)) for row in range(3)
    ]


def _fixture() -> dict:
    path = files("task_worker_api").joinpath("fixtures", "coordinate_fixture_v1.json")
    return json.loads(path.read_text(encoding="utf-8"))


def test_spatial_reconstruction_schema_registered():
    assert TaskType.SPATIAL_RECONSTRUCTION.value == "spatial_recon"
    assert (
        TASK_PARAMS_SCHEMAS[TaskType.SPATIAL_RECONSTRUCTION]
        is SpatialReconstructionParams
    )


def test_spatial_reconstruction_params_roundtrip():
    params = SpatialReconstructionParams.model_validate(_valid_payload())
    assert params.capture_root_rel.endswith(params.capture_hash)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("case_guid", " "),
        ("capture_hash", "../escape"),
        ("capture_hash", "nested/hash"),
        (
            "capture_root_rel",
            "/case_data/spatial_capture/a88091c7a88091c7a88091c7a88091c7",
        ),
        ("capture_root_rel", "case_data/spatial_capture/other"),
        ("coordinate_frame", "opencv_world"),
    ],
)
def test_spatial_reconstruction_rejects_invalid_contract(field: str, value: str):
    payload = _valid_payload()
    payload[field] = value
    with pytest.raises(ValidationError):
        SpatialReconstructionParams.model_validate(payload)


def test_spatial_reconstruction_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        SpatialReconstructionParams.model_validate(
            {**_valid_payload(), "process_res": 1008}
        )


def test_coordinate_fixture_camera_projection_and_composition():
    fixture = _fixture()
    tracking_from_anchor = _matrix(fixture["tracking_from_anchor"])
    assert fixture["coordinate_frame"] == "syngar_anchor_v1"
    assert len(fixture["frames"]) == 2

    for frame in fixture["frames"]:
        anchor_from_camera = _matrix(frame["expected_anchor_from_camera"])
        expected_tracking = _matrix(frame["tracking_from_camera"])
        composed = _multiply(tracking_from_anchor, anchor_from_camera)
        for actual_row, expected_row in zip(composed, expected_tracking):
            assert actual_row == pytest.approx(expected_row)

        fx, fy = frame["intrinsics"][0][0], frame["intrinsics"][1][1]
        cx, cy = frame["intrinsics"][0][2], frame["intrinsics"][1][2]
        for expected in frame["expected_anchor_points"]:
            u, v = expected["pixel_uv"]
            depth = frame["depth_m"][v][u]
            camera_point = [(u - cx) * depth / fx, -(v - cy) * depth / fy, depth]
            assert _transform(anchor_from_camera, camera_point) == pytest.approx(
                expected["point"]
            )


def test_coordinate_fixture_rotated_volume_crop():
    volume = _fixture()["capture_volume"]
    anchor_from_volume = _matrix(volume["anchor_from_volume"])
    rotation = [row[:3] for row in anchor_from_volume[:3]]
    translation = [row[3] for row in anchor_from_volume[:3]]
    limits = [size / 2 + volume["crop_padding_m"] for size in volume["size_m"]]

    for case in volume["crop_cases"]:
        delta = [
            value - translation[index]
            for index, value in enumerate(case["anchor_point"])
        ]
        local = [
            sum(rotation[row][column] * delta[row] for row in range(3))
            for column in range(3)
        ]
        assert local == pytest.approx(case["expected_volume_point"])
        assert (
            all(abs(value) <= limits[index] for index, value in enumerate(local))
            is case["inside"]
        )
