"""Frozen interactive prompts shared by the API, admission and inference worker."""
import base64
import struct
from typing import Annotated, Literal

from pydantic import Field, model_validator

from ._base import TaskParamsBase

Coordinate = Annotated[int, Field(ge=0, strict=True)]


class InteractiveSeed(TaskParamsBase):
    slice: Coordinate
    obj_id: int = Field(ge=1, le=254, strict=True)
    mask_png_b64: str | None = Field(default=None, max_length=4 * 1024 * 1024)
    box: tuple[Coordinate, Coordinate, Coordinate, Coordinate] | None = None
    points: list[tuple[Coordinate, Coordinate]] | None = Field(default=None, min_length=1, max_length=4096)
    point_labels: list[Literal[0, 1]] | None = None

    @model_validator(mode="after")
    def one_prompt(self):
        if sum(value is not None for value in (self.mask_png_b64, self.box, self.points)) != 1:
            raise ValueError("Each seed requires exactly one mask, box or point prompt")
        if self.point_labels is not None and (self.points is None or len(self.point_labels) != len(self.points)):
            raise ValueError("Point labels must match the points")
        return self


class InteractiveSeeds(TaskParamsBase):
    dims: tuple[Annotated[int, Field(gt=0)], Annotated[int, Field(gt=0)], Annotated[int, Field(gt=0)]]
    axis: int = Field(ge=0, le=2, strict=True)
    depth_range: tuple[Coordinate, Coordinate]
    seeds: list[InteractiveSeed] = Field(min_length=1, max_length=1024)

    @model_validator(mode="after")
    def geometry(self):
        x, y, z = self.dims
        width, height, depth = ((x, y, z), (x, z, y), (y, z, x))[self.axis]
        lo, hi = self.depth_range
        if not 0 <= lo <= hi < depth:
            raise ValueError("Propagation range is outside the volume")
        seen = set()
        encoded_bytes = 0
        for seed in self.seeds:
            key = (seed.obj_id, seed.slice)
            if key in seen or not lo <= seed.slice <= hi:
                raise ValueError("Seed is duplicated or outside the propagation range")
            seen.add(key)
            if seed.box is not None:
                left, top, right, bottom = seed.box
                if not (left < right <= width and top < bottom <= height):
                    raise ValueError("Seed box is outside the slice")
            if seed.points is not None and any(col >= width or row >= height for col, row in seed.points):
                raise ValueError("Seed point is outside the slice")
            if seed.mask_png_b64 is not None:
                encoded = seed.mask_png_b64.removeprefix("data:image/png;base64,")
                encoded_bytes += len(encoded)
                if encoded_bytes > 16 * 1024 * 1024:
                    raise ValueError("Seed masks exceed the request budget")
                png = base64.b64decode(encoded, validate=True)
                if (png[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
                        or len(png) < 24 or struct.unpack(">II", png[16:24]) != (width, height)):
                    raise ValueError("Seed mask must be a PNG matching the slice dimensions")
        if len(self.model_dump_json().encode()) > 16 * 1024 * 1024:
            raise ValueError("Interactive prompts exceed the request budget")
        return self
