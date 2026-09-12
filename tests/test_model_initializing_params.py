import pytest
from pydantic import ValidationError

from task_worker_api.schemas import ModelInitializingParams


def test_mesh_preprocessing_options_validate_without_importing_blender():
    base = dict(job_id="job", input_path="mesh.stl", base_name="mesh")
    params = ModelInitializingParams(**base, remove_interior=True, yup=False)
    assert params.remove_interior and not params.yup
    assert params.convex_hull_target_faces == 500 and params.preview_max_triangles == 120_000
    assert "preview_remesher" not in params.model_dump()
    for invalid in (dict(convex_hull_target_faces=2_000_001), dict(preview_max_triangles=120_001),
                    dict(convex_hull_margin=float("inf")), dict(preview_remesher="auto"),
                    dict(qremeshify_scale_factor=1.0), dict(qremeshify_time_limit=200), dict(unknown=True)):
        with pytest.raises(ValidationError):
            ModelInitializingParams(**base, **invalid)
