"""Canonical filename conventions for task-produced artifacts.

Shared between the Blender worker (which writes these filenames) and
SynPusher backend (which predicts them at enqueue time). Treating the
convention as a shared helper prevents the two repos from drifting.
"""
from __future__ import annotations


def preview_filename(base_name: str) -> str:
    """Canonical filename for model_initializing's simplified preview GLB."""
    return f"{base_name}_simplified_preview.glb"


def finalized_filename(base_name: str) -> str:
    """Canonical filename for cinematic_baking's finalized GLB."""
    return f"{base_name}_finalized.glb"
