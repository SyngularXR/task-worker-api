"""A warm interactive segmentation session: one admitted attempt, many rounds.

The task freezes the volume once; the worker encodes it and then answers
rounds the backend relays from the browser until a ``close`` round, its idle
limit or its execution deadline. A ``propagate`` round carries the frozen
prompts of every prompted plane at once; a ``click`` round asks for the region
under a few points on one frame only.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from ._base import TaskParamsBase
from .interactive_segmentation import Coordinate, InteractiveSeeds

Dims = tuple[Annotated[int, Field(gt=0)], Annotated[int, Field(gt=0)], Annotated[int, Field(gt=0)]]


def plane_size(dims, axis):
    """(width, height, depth) of ``axis`` for XYZ ``dims``, as InteractiveSeeds uses."""
    x, y, z = dims
    return ((x, y, z), (x, z, y), (y, z, x))[axis]


class InteractiveSessionParams(TaskParamsBase):
    """Input for a warm session; rounds arrive through the attempt, not here."""

    input_path: str = Field(..., description="NIfTI volume on the shared volume.")
    output_dir: str = Field(..., description="Server-minted shared dir of the session.")
    case_id: int
    dicom_id: int
    dims: Dims
    modality: Literal["mri", "ct"]
    window_center: float | None = Field(default=None, allow_inf_nan=False)
    window_width: float | None = Field(default=None, allow_inf_nan=False)
    volume_version: str | None = Field(default=None, max_length=128)
    idle_seconds: int = Field(default=300, ge=30, le=1800)


class InteractiveClick(TaskParamsBase):
    dims: Dims
    axis: int = Field(ge=0, le=2, strict=True)
    slice: Coordinate
    obj_id: int = Field(ge=1, le=254, strict=True)
    points: list[tuple[Coordinate, Coordinate]] = Field(min_length=1, max_length=64)
    point_labels: list[Literal[0, 1]]

    @model_validator(mode="after")
    def on_the_slice(self):
        width, height, depth = plane_size(self.dims, self.axis)
        if self.slice >= depth:
            raise ValueError("Click slice is outside the volume")
        if len(self.point_labels) != len(self.points):
            raise ValueError("Point labels must match the points")
        if any(col >= width or row >= height for col, row in self.points):
            raise ValueError("Click point is outside the slice")
        return self


class InteractiveRound(TaskParamsBase):
    """One request to a warm session."""

    round_id: int = Field(ge=1, strict=True)
    kind: Literal["propagate", "click", "close"]
    planes: list[InteractiveSeeds] | None = Field(default=None, min_length=1, max_length=3)
    click: InteractiveClick | None = None

    @model_validator(mode="after")
    def one_request(self):
        if (self.planes is not None) != (self.kind == "propagate") or (self.click is not None) != (self.kind == "click"):
            raise ValueError("A round carries planes to propagate, one click, or nothing to close")
        if self.planes is not None:
            if len({plane.axis for plane in self.planes}) != len(self.planes):
                raise ValueError("Each plane appears once in a round")
            if len({plane.dims for plane in self.planes}) != 1:
                raise ValueError("All planes of a round share the volume dimensions")
        if len(self.model_dump_json().encode()) > 32 * 1024 * 1024:
            raise ValueError("Interactive round exceeds the request budget")
        return self


class InteractiveRoundReply(TaskParamsBase):
    """The worker's answer to a round: ``result`` details plus small files.

    ``files`` maps safe filenames to base64 payloads whose format the round's
    consumer defines (the backend validates them before the browser sees them).
    """

    round_id: int = Field(ge=1, strict=True)
    status: Literal["done", "failed"]
    error: str | None = Field(default=None, max_length=2000)
    result: dict = Field(default_factory=dict)
    files: dict[str, str] = Field(default_factory=dict, max_length=8)

    @model_validator(mode="after")
    def bounded(self):
        if (self.status == "failed") != (self.error is not None) or (self.status == "failed" and self.files):
            raise ValueError("A failed round carries an error and no files")
        if len(self.model_dump_json().encode()) > 64 * 1024 * 1024:
            raise ValueError("Interactive round reply exceeds the response budget")
        return self
