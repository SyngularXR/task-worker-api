"""Params schema for DEPLOY_CASE tasks.

Handled by the assetbundle-builder worker. `content_path` is the absolute
path to the exported case content folder on the shared volume, written by
the backend's export_case_model_collection() before the task is created.
`build_target` is passed as-is to the Unity CLI's -buildTarget flag.
"""
from __future__ import annotations
from typing import Literal

from pydantic import Field, model_validator
from ._base import TaskParamsBase


class PrepareDeployParams(TaskParamsBase):
    """Assemble the immutable recipe staged by the backend admission service."""

    recipe_path: Literal["recipe.json"] = "recipe.json"


class DeployCaseParams(TaskParamsBase):
    """Input for the assetbundle-builder worker's deploy_case handler."""

    content_path: str | None = None  # retained for currently deployed workers
    snapshot_path: str | None = None
    output_path: str | None = None
    case_guid: str | None = None
    deploy_hash: str | None = None
    build_target: str = "Android"
    platform: str = "android"
    schema_version: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def require_snapshot(self) -> "DeployCaseParams":
        if not self.snapshot_path and not self.content_path:
            raise ValueError("snapshot_path is required")
        return self
