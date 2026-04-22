"""Params schema for MODEL_INITIALIZING tasks.

Matches the shape produced by
services/backend/src/utils/extra_model_registry.py:_build_task_params.
"""
from __future__ import annotations

from pydantic import Field

from ._base import TaskParamsBase


class ModelInitializingParams(TaskParamsBase):
    """Input for the Blender worker's model_initializing handler."""

    job_id: str = Field(..., description="Stable job identifier for metadata mirror.")
    input_path: str = Field(..., description="Absolute STL path on the shared volume.")
    base_name: str = Field(..., description="Filename stem for outputs.")
