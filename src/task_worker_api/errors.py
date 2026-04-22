"""Error classes raised by the SDK and handlers."""
from __future__ import annotations


class TaskCancelled(Exception):
    """Raised inside a handler when its task has been cancelled.

    The Worker catches this and reports the task as cancelled cooperatively
    (via `PUT /tasks/{id}/fail` with a cancel-reason) rather than FAILED.
    """


class TaskParamsError(Exception):
    """Handler received params that failed schema validation on claim.

    Distinguished from arbitrary handler exceptions so the Worker can
    report a clean protocol-error message rather than a traceback dump.
    """


class ProtocolError(Exception):
    """Raised when the worker-backend wire protocol breaks invariants.

    Examples: backend returned a task with an unknown task_type; a schema
    that the worker's pinned package version cannot decode; an HTTP
    response shape that doesn't match the expected envelope.
    """
