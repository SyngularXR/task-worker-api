"""Base class for every Task.params schema.

`extra="forbid"` is the single most important line in this package: it
turns "frontend sent `input_file` instead of `input_path`" from a silent
worker crash into a 422 at enqueue time with a clear error message.

Naming conventions (enforced by a linter in this package's CI; not by
Pydantic itself):

- Single input on a shared volume: ``input_path: str``
- Multiple inputs on a shared volume: ``input_paths: dict[str, str]``
- Remote-worker inputs: ``input_files: dict[str, str]``
- Never ``input_file`` (singular). Never ``output_dir`` (the worker
  dispatcher supplies that as a separate argument).
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class TaskParamsBase(BaseModel):
    """Base for every Task.params schema. Rejects unknown fields."""

    model_config = ConfigDict(extra="forbid")
