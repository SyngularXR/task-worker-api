"""Algorithm-neutral, admitted replay contract; live sessions are not supported yet."""
import json
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from ._base import TaskParamsBase


class VisualTrackingParams(TaskParamsBase):
    """Replay candidate-only inputs staged from an immutable admission manifest.

    Stage sequence.json at the input root and preserve its relative asset paths.
    Ground truth is evaluator-only and must never be included in these inputs.
    The installed handler allowlists adapters; an ID is not a Python import path.
    """

    schema_version: Literal[1] = 1
    mode: Literal["replay"] = "replay"
    sequence_path: Literal["sequence.json"] = "sequence.json"
    adapter_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    adapter_params: dict[str, Any] = Field(default_factory=dict)
    frame_timeout_seconds: float = Field(default=2, strict=True, gt=0, le=60, allow_inf_nan=False)
    run_timeout_seconds: float = Field(default=300, strict=True, gt=0, le=3600, allow_inf_nan=False)

    @field_validator("adapter_params")
    @classmethod
    def bounded_json(cls, value):
        try:
            encoded = json.dumps(value, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValueError("adapter_params must be finite JSON") from exc
        if len(encoded) > 65536:
            raise ValueError("adapter_params exceeds 64 KiB")
        return value

    @model_validator(mode="after")
    def ordered_timeouts(self):
        if self.run_timeout_seconds < self.frame_timeout_seconds:
            raise ValueError("run timeout must cover at least one frame timeout")
        return self
