"""SegmentationParams (widened in 0.9.0) — full one-shot field coverage."""
import pytest

from task_worker_api.enums import TaskType
from task_worker_api.schemas import TASK_PARAMS_SCHEMAS


def _schema():
    return TASK_PARAMS_SCHEMAS[TaskType.SEGMENTATION]


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
    with pytest.raises(Exception):
        _schema().model_validate(
            {
                "input_path": "/s/x",
                "model": "vista3d",
                "output_dir": "/s/t",
                "result_mask_id": "s",
                "bogus_field": 1,
            }
        )


def test_output_dir_required():
    with pytest.raises(Exception):
        _schema().model_validate({"input_path": "/s/x", "model": "vista3d"})


def test_finalize_segmentation_enum_exists_but_not_in_registry():
    assert TaskType.FINALIZE_SEGMENTATION.value == "finalize_segment"
    assert len(TaskType.FINALIZE_SEGMENTATION.value) <= 20
    assert TaskType.FINALIZE_SEGMENTATION not in TASK_PARAMS_SCHEMAS
