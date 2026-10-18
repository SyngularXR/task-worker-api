import pytest
from pydantic import ValidationError
from task_worker_api.enums import TaskType
from task_worker_api.schemas import TASK_PARAMS_SCHEMAS, VolumeResampleParams


def test_resampling_contract_excludes_diffusion_and_enhancement():
    params = dict(input_path="input.nii.gz", modality="MRI", dicom_id=1, method="ram2",
                  job_id="e8cfaa57-10cb-4be8-9603-2fa30c41b114")
    assert TASK_PARAMS_SCHEMAS[TaskType.VOLUME_RESAMPLE].model_validate(params).modality == "MRI"
    assert VolumeResampleParams.model_validate(params).acquisition_plane == "axial"
    for plane in ("axial", "coronal", "sagittal"):
        assert VolumeResampleParams.model_validate({**params, "acquisition_plane": plane}).acquisition_plane == plane
    for extra in ({"modality": "DTI"}, {"acquisition_plane": "oblique"}, {"enable_dicom_enhance": True}, {"target_spacing": 0.5}):
        with pytest.raises(ValidationError):
            VolumeResampleParams.model_validate({**params, **extra})


def test_phase_files_rejects_reserved_volume_key():
    params = dict(input_path="input.nii.gz", modality="MRI", dicom_id=1, method="ram2",
                  job_id="e8cfaa57-10cb-4be8-9603-2fa30c41b114")
    with pytest.raises(ValidationError, match='phase_files key "volume" is reserved for input_path'):
        VolumeResampleParams.model_validate({**params, "phase_files": {"volume": "x.nii.gz"}})


def test_phase_files_round_trip():
    params = dict(input_path="input.nii.gz", modality="MRI", dicom_id=1, method="ram2",
                  job_id="e8cfaa57-10cb-4be8-9603-2fa30c41b114")
    model = VolumeResampleParams.model_validate({**params, "phase_files": {"phase_1": "/data/p.nii.gz"}})
    dumped = model.model_dump(mode="json")
    assert dumped["phase_files"] == {"phase_1": "/data/p.nii.gz"}
    assert VolumeResampleParams.model_validate(dumped) == model
