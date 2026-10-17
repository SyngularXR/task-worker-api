"""SegmentationParams (widened in 0.9.0) — full one-shot field coverage."""
import pytest
from pydantic import ValidationError

from task_worker_api.enums import TaskType
from task_worker_api.schemas import TASK_PARAMS_SCHEMAS


_FINITE_FLOAT_FIELDS = (
    "min_volume_cm3",
    "threshold",
    "nms_iou",
    "window_center",
    "window_width",
    "volume_threshold",
    "grow_gradient_threshold",
    "grow_intensity_sigma",
)


def _schema():
    return TASK_PARAMS_SCHEMAS[TaskType.SEGMENTATION]


@pytest.mark.parametrize(
    "model,field,value,error_type",
    [
        ("vista3d", "grow_max_distance", -1, "greater_than_equal"),
        ("vista3d", "grow_max_distance", 21, "less_than_equal"),
        ("vista3d", "grow_shrink_voxels", -1, "greater_than_equal"),
        ("vista3d", "grow_shrink_voxels", 21, "less_than_equal"),
        ("medsam2", "axis", -1, "greater_than_equal"),
        ("medsam2", "axis", 3, "less_than_equal"),
        ("vista3d", "grow_gradient_threshold", -0.1, "greater_than_equal"),
        ("vista3d", "grow_intensity_sigma", -0.1, "greater_than_equal"),
        ("medsam2", "seed_slice", -1, "greater_than_equal"),
    ],
)
def test_rejects_out_of_range(model, field, value, error_type):
    with pytest.raises(ValidationError) as exc:
        _schema()(input_path="/p", model=model, output_dir="/o", **{field: value})
    assert exc.value.errors()[0]["loc"] == (field,)
    assert exc.value.errors()[0]["type"] == error_type


@pytest.mark.parametrize(
    "field,value",
    [
        ("grow_max_distance", 0),
        ("grow_max_distance", 20),
        ("grow_shrink_voxels", 0),
        ("grow_shrink_voxels", 20),
        ("axis", 0),
        ("axis", 2),
        ("grow_gradient_threshold", 0.0),
        ("grow_intensity_sigma", 0.0),
        ("seed_slice", 0),
    ],
)
def test_accepts_bounds(field, value):
    p = _schema()(input_path="/p", model="vista3d", output_dir="/o", **{field: value})
    assert getattr(p, field) == value


def test_seed_slice_none_default_and_explicit():
    params = dict(input_path="/p", model="vista3d", output_dir="/o")
    assert _schema()(**params).seed_slice is None
    assert _schema()(**params, seed_slice=None).seed_slice is None


def test_synpusher_explicit_values_validate():
    values = dict(grow_max_distance=2, grow_shrink_voxels=4, grow_intensity_sigma=5.0, axis=0)
    p = _schema()(input_path="/p", model="vista3d", output_dir="/o", **values)
    assert p.model_dump(include=set(values)) == values


def test_synpusher_effective_vista3d_defaults_validate():
    values = dict(grow_max_distance=0, grow_shrink_voxels=0, grow_intensity_sigma=0.0, axis=0)
    p = _schema()(input_path="/p", model="vista3d", output_dir="/o", **values)
    assert p.model_dump(include=set(values)) == values


@pytest.mark.parametrize(
    "model,field,value,error_type",
    [
        ("vista3d", "axis", 3, "less_than_equal"),
        ("vista3d", "seed_slice", -1, "greater_than_equal"),
        ("medsam2", "grow_max_distance", 21, "less_than_equal"),
        ("medsam2", "grow_shrink_voxels", 21, "less_than_equal"),
        ("medsam2", "grow_gradient_threshold", -0.1, "greater_than_equal"),
        ("medsam2", "grow_intensity_sigma", -0.1, "greater_than_equal"),
    ],
)
def test_bounds_apply_to_unused_model_fields(model, field, value, error_type):
    with pytest.raises(ValidationError) as exc:
        _schema()(input_path="/p", model=model, output_dir="/o", **{field: value})
    assert exc.value.errors()[0]["loc"] == (field,)
    assert exc.value.errors()[0]["type"] == error_type


@pytest.mark.parametrize("field", _FINITE_FLOAT_FIELDS)
@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), float("-inf")], ids=["nan", "inf", "-inf"],
)
def test_rejects_non_finite_float(field, value):
    with pytest.raises(ValidationError) as exc:
        _schema()(input_path="/p", model="vista3d", output_dir="/o", **{field: value})
    assert exc.value.errors()[0]["loc"] == (field,)
    assert exc.value.errors()[0]["type"] == "finite_number"


def test_accepts_negative_window_center_and_keeps_none():
    params = dict(input_path="/p", model="vista3d", output_dir="/o")
    p = _schema()(**params, window_center=-6.0)
    assert p.window_center == -6.0
    assert p.window_width is None
    assert p.threshold == 0.5
    p = _schema()(**params, window_center=None, window_width=None)
    assert p.window_center is None
    assert p.window_width is None


async def test_worker_rejects_nan_threshold_before_handler(make_worker, fake_client):
    task = fake_client.queue_task(
        task_type=TaskType.SEGMENTATION,
        params={
            "input_path": "/p", "model": "vista3d", "output_dir": "/o",
            "threshold": float("nan"),
        },
    )
    called = []

    async def handler(ctx, params):
        called.append(params)
        return {}

    worker = make_worker(client=fake_client, handlers={TaskType.SEGMENTATION: handler})
    await worker.run_one()

    assert called == []
    assert fake_client.completed_tasks == []
    assert len(fake_client.failed_tasks) == 1
    failure = fake_client.failed_tasks[0]
    assert failure["task_id"] == task.id
    assert "schema validation" in failure["error"]
    assert "threshold" in failure["error"]


def test_medsam3_params_roundtrip():
    p = _schema().model_validate(
        {
            "input_path": "/app/shared/temp/seg/x.nii.gz",
            "model": "medsam3",
            "output_dir": "/app/shared/temp/seg/task_g",
            "result_mask_id": "seg-1",
            "case_id": 7,
            "dicom_id": 53,
            "result_frame": "primary",
            "prompt": "tumor",
            "axis": 0,
            "threshold": 0.5,
            "nms_iou": 0.5,
            "window_center": 40.0,
            "window_width": 400.0,
        }
    )
    assert p.prompt == "tumor"
    assert p.output_dir.endswith("task_g")
    assert p.window_center == 40.0


def test_vista3d_params_roundtrip():
    p = _schema().model_validate(
        {
            "input_path": "/s/x.nii.gz",
            "model": "vista3d",
            "output_dir": "/s/t",
            "result_mask_id": "seg-2",
            "target": "liver",
            "modality": "CT_BODY",
            "label_prompt": [1, 3],
            "boundary_smoothing": True,
        }
    )
    assert p.label_prompt == [1, 3]
    assert p.target == "liver"


def test_medsam2_oneshot_params_roundtrip():
    p = _schema().model_validate(
        {
            "input_path": "/s/x.nii.gz",
            "model": "medsam2",
            "output_dir": "/s/t",
            "result_mask_id": "seg-3",
            "seed_slice": 5,
            "seed_box": [0, 0, 4, 4],
            "medsam2_modality": "mri",
        }
    )
    assert p.seed_slice == 5
    assert p.seed_box == [0, 0, 4, 4]


def test_labels_deprecated_alias_accepted():
    p = _schema().model_validate(
        {
            "input_path": "/s/x",
            "model": "vista3d",
            "output_dir": "/s/t",
            "result_mask_id": "s",
            "labels": ["1", "2"],
        }
    )
    assert p.labels == ["1", "2"]


def test_forbid_extra():
    with pytest.raises(ValidationError) as exc:
        _schema().model_validate(
            {
                "input_path": "/s/x",
                "model": "vista3d",
                "output_dir": "/s/t",
                "result_mask_id": "s",
                "bogus_field": 1,
            }
        )
    assert exc.value.errors()[0]["loc"] == ("bogus_field",)
    assert exc.value.errors()[0]["type"] == "extra_forbidden"


def test_output_dir_required():
    with pytest.raises(Exception):
        _schema().model_validate({"input_path": "/s/x", "model": "vista3d"})


def test_finalize_segmentation_enum_exists_but_not_in_registry():
    assert TaskType.FINALIZE_SEGMENTATION.value == "finalize_segment"
    assert len(TaskType.FINALIZE_SEGMENTATION.value) <= 20
    assert TaskType.FINALIZE_SEGMENTATION not in TASK_PARAMS_SCHEMAS
