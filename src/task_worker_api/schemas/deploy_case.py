"""Params schema for DEPLOY_CASE tasks.

Handled by the assetbundle-builder worker. `content_path` is the absolute
path to the exported case content folder on the shared volume, written by
the backend's export_case_model_collection() before the task is created.
`build_target` is passed as-is to the Unity CLI's -buildTarget flag.
"""
from __future__ import annotations

from ._base import TaskParamsBase


class DeployCaseParams(TaskParamsBase):
    """Input for the assetbundle-builder worker's deploy_case handler."""

    content_path: str  # absolute path to case content folder on shared volume
    build_target: str = "Android"
