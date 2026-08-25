"""Params schema for case-owned spatial reconstruction tasks."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator

from ._base import TaskParamsBase


class SpatialReconstructionParams(TaskParamsBase):
    """Locate one immutable capture under the worker's shared-data root."""

    case_guid: str = Field(min_length=1)
    capture_hash: str = Field(pattern=r"^[0-9a-f]{32}$")
    capture_root_rel: str = Field(min_length=1)
    coordinate_frame: Literal["syngar_anchor_v1"]

    @field_validator("case_guid", "capture_hash")
    @classmethod
    def reject_blank_identifiers(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError(
                "identifier must be non-blank without surrounding whitespace"
            )
        return value

    @model_validator(mode="after")
    def require_canonical_capture_root(self) -> SpatialReconstructionParams:
        expected = f"case_data/spatial_capture/{self.capture_hash}"
        if self.capture_root_rel != expected:
            raise ValueError(f"capture_root_rel must equal {expected}")
        return self
