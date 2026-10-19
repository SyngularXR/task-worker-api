import base64
import struct

import pytest
from pydantic import ValidationError

from task_worker_api.schemas.interactive_segmentation import InteractiveSeeds


def bundle(**updates):
    return dict(dims=[4, 5, 20], axis=0, depth_range=[0, 19],
                seeds=[dict(slice=2, obj_id=1, points=[[1, 2]], point_labels=[1]),
                       dict(slice=17, obj_id=1, box=[0, 0, 3, 4])], **updates)


def test_distant_seeds_and_geometry_round_trip():
    value = InteractiveSeeds.model_validate(bundle())
    assert InteractiveSeeds.model_validate_json(value.model_dump_json()) == value
    assert [seed.slice for seed in value.seeds] == [2, 17]


@pytest.mark.parametrize("change", [
    {"depth_range": [0, 20]}, {"depth_range": [4, 19]}, {"axis": 3},
    {"seeds": []},
    {"seeds": [dict(slice=2, obj_id=255, points=[[1, 2]])]},
    {"seeds": [dict(slice=2, obj_id=1, points=[[4, 2]])]},
    {"seeds": [dict(slice=2, obj_id=1, points=[[1, 2]], point_labels=[])]},
    {"seeds": [dict(slice=2, obj_id=1, box=[0, 0, 5, 4])]},
    {"seeds": [dict(slice=2, obj_id=1, box=[0, 0, 2, 2], points=[[1, 1]])]},
    {"seeds": [dict(slice=2, obj_id=1, mask_png_b64="bad")]},
])
def test_invalid_prompts_are_rejected(change):
    with pytest.raises((ValidationError, ValueError)):
        InteractiveSeeds.model_validate(bundle() | change)


def test_duplicate_seeds_are_rejected():
    value = bundle()
    value["seeds"].append(value["seeds"][0])
    with pytest.raises(ValidationError, match="duplicated"):
        InteractiveSeeds.model_validate(value)


@pytest.mark.parametrize("axis,size", [(0, (4, 5)), (1, (4, 20)), (2, (5, 20))])
def test_png_dimensions_follow_the_view(axis, size):
    png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + struct.pack(">II", *size)
    value = bundle() | dict(axis=axis, depth_range=[0, 3], seeds=[dict(
        slice=2, obj_id=1, mask_png_b64="data:image/png;base64," + base64.b64encode(png).decode())])
    assert InteractiveSeeds.model_validate(value).axis == axis
