"""Shared contract and worker SDK for the SynPusher task queue.

v0.1.0 — minimum-viable scaffold. See README for status.
"""
from .enums import TaskType, TaskStatus
from .errors import TaskCancelled, TaskParamsError, ProtocolError
from .schemas import TASK_PARAMS_SCHEMAS, TaskParamsBase

__version__ = "0.1.0"

__all__ = [
    "TaskType",
    "TaskStatus",
    "TaskCancelled",
    "TaskParamsError",
    "ProtocolError",
    "TASK_PARAMS_SCHEMAS",
    "TaskParamsBase",
]
