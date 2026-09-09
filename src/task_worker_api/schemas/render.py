"""Frozen scene contract for the native renderer."""
from pydantic import Field, field_validator

from ._base import TaskParamsBase


class RenderParams(TaskParamsBase):
    config_path: str
    job_id: str
    config_hash: str
    colmap_only: bool = False
    resume: bool = False
    dry_render: int | None = Field(default=None, gt=0, strict=True)
    chain_gs: bool = False
    gs_quality: str | None = None
    gs_iterations: int | None = None
    gs_max_splats: int | None = None
    gs_sh_degree: int | None = None
    gs_dense_init: bool | None = None

    @field_validator("config_path")
    @classmethod
    def relative_scene_config(cls, value):
        from ..resources import InputArtifact

        InputArtifact(filename="config.json", path=value, sha256="0" * 64, size_bytes=0)
        if not value.startswith("render_grid/") or not value.endswith("/config.json"):
            raise ValueError("config_path must name a render_grid scene config")
        return value
