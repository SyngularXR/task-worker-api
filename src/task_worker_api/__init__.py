"""Shared contract and worker SDK for the SynPusher task queue.

v0.2.0 adds the runtime: BackendClient, Worker, CancelGuard, ProgressReporter,
file transfer, hybrid-mode runner, and testing fixtures.
"""
from .cancel import CancelGuard
from .client import BackendClient
from .context import ClaimedTask, FileContext, TaskContext
from .enums import TaskStatus, TaskType
from .errors import ProtocolError, TaskCancelled, TaskParamsError
from .files import prepare_inputs, upload_outputs
from .progress import ProgressReporter
from .schemas import TASK_PARAMS_SCHEMAS, TaskParamsBase
from .worker import Worker, run_hybrid

__version__ = "0.3.0"

__all__ = [
    "__version__",
    # Enums
    "TaskType",
    "TaskStatus",
    # Errors
    "TaskCancelled",
    "TaskParamsError",
    "ProtocolError",
    # Schemas
    "TASK_PARAMS_SCHEMAS",
    "TaskParamsBase",
    # Context
    "ClaimedTask",
    "FileContext",
    "TaskContext",
    # Runtime
    "BackendClient",
    "CancelGuard",
    "ProgressReporter",
    "Worker",
    "run_hybrid",
    "prepare_inputs",
    "upload_outputs",
]
