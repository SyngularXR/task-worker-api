"""Typed wrappers workers receive from the SDK.

``ClaimedTask`` is what ``BackendClient.claim_next()`` returns — a typed
view of the Task row, with ``status`` as a ``TaskStatus`` enum instead of
the raw int the backend emits for backwards compat. ``TaskContext`` is
what handlers receive: id + files + progress, all typed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

from .enums import TaskStatus, TaskType

if TYPE_CHECKING:  # pragma: no cover — avoids circular import at runtime
    from .progress import ProgressReporter


@dataclass(frozen=True)
class ClaimedTask:
    """A task row as returned by ``BackendClient.claim_next()``."""

    id: int
    task_type: TaskType
    case_id: Optional[int]
    item_key: str
    status: TaskStatus       # enum, never raw int
    params: dict             # raw dict; validation happens in Worker.run_forever
    worker_id: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict) -> "ClaimedTask":
        """Construct from the backend's ``_task_to_dict`` envelope."""
        return cls(
            id=data["id"],
            task_type=TaskType(data["task_type"]),
            case_id=data.get("case_id"),
            item_key=data.get("item_key", ""),
            status=TaskStatus(int(data["status"])),
            params=data.get("params") or {},
            worker_id=data.get("worker_id"),
        )


@dataclass
class FileContext:
    """Per-task input/output directories staged by ``prepare_inputs``."""

    input_dir: Path
    output_dir: Path
    primary_path: Path                    # main input file
    all_paths: dict[str, Path] = field(default_factory=dict)


@dataclass
class TaskContext:
    """What handlers receive. Everything they need, typed."""

    task: ClaimedTask
    files: FileContext
    progress: "ProgressReporter"
