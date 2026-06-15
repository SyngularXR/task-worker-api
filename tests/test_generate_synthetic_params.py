"""Tests for the GENERATE_SYNTHETIC params schema + enum registration."""
from __future__ import annotations

import pytest

from task_worker_api.enums import TaskType
from task_worker_api.schemas import TASK_PARAMS_SCHEMAS


def _valid_payload() -> dict:
    return {
        "sources": [
            {"dicom_id": 53, "nifti_path": "/app/shared/a.nii.gz",
             "orientation": "sagittal", "sequence_key": "t2", "name": "sa"},
            {"dicom_id": 54, "nifti_path": "/app/shared/b.nii.gz",
             "orientation": "coronal", "sequence_key": "t2", "name": "co"},
        ],
        "source_dicom_ids": [53, 54],
        "reference_dicom_id": 53,
        "reference_size": [16, 262, 436],
        "synthetic_roi": {"mode": "full"},
        "staging_dir": "/app/shared/case_data/dicom_data/g/staging",
        "output_dir": "/app/shared/case_data/dicom_data/g",
        "output_filename": "g.nii.gz",
        "name": "Synthetic_X",
        "author_id": 1,
        "niftymic": {"isotropic_resolution": "1.0"},
    }


def test_generate_synthetic_schema_registered():
    assert TaskType.GENERATE_SYNTHETIC in TASK_PARAMS_SCHEMAS


def test_finalize_synthetic_not_registered():
    # Backend-local only — must NOT be claimable via the public task API.
    assert TaskType.FINALIZE_SYNTHETIC not in TASK_PARAMS_SCHEMAS


def test_generate_synthetic_params_roundtrip():
    schema = TASK_PARAMS_SCHEMAS[TaskType.GENERATE_SYNTHETIC]
    p = schema.model_validate(_valid_payload())
    assert p.reference_dicom_id == 53
    assert len(p.sources) == 2
    assert p.sources[0].nifti_path == "/app/shared/a.nii.gz"
    assert p.source_dicom_ids == [53, 54]


def test_generate_synthetic_params_forbid_extra():
    schema = TASK_PARAMS_SCHEMAS[TaskType.GENERATE_SYNTHETIC]
    bad = _valid_payload()
    bad["bogus_field"] = 1
    with pytest.raises(Exception):
        schema.model_validate(bad)


def test_generate_synthetic_requires_two_to_three_sources():
    schema = TASK_PARAMS_SCHEMAS[TaskType.GENERATE_SYNTHETIC]
    one = _valid_payload()
    one["sources"] = one["sources"][:1]
    with pytest.raises(Exception):
        schema.model_validate(one)
