"""Admission HTTP messages and authenticated host observations.

Reporter keys belong to the host supervisor, never to polling workers. A worker
may forward an observation but cannot change its authority, epoch or payload.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from .resources import (
    AttemptOwnership, ClaimRequest, CleanupEvidence, HostSnapshot, Positive,
    ResourceModel,
)


def observation_signature(kind: str, authority_id: UUID, epoch: int, payload: dict, key: bytes) -> str:
    if len(key) < 32:
        raise ValueError("reporter signing keys require at least 32 bytes")
    message = json.dumps(
        {"kind": kind, "authority_id": str(authority_id), "epoch": epoch, "payload": payload},
        sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


Signature = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", repr=False)]


class SignedHostReport(ResourceModel):
    authority_id: UUID
    epoch: Positive
    report: HostSnapshot
    signature: Signature


class CleanupObservation(ResourceModel):
    host_id: UUID
    issued_at: AwareDatetime
    evidence: CleanupEvidence


class SignedCleanup(ResourceModel):
    authority_id: UUID
    epoch: Positive
    observation: CleanupObservation
    signature: Signature


class ClaimBody(ClaimRequest):
    host_report: SignedHostReport


class ReadyBody(ResourceModel):
    protocol_version: Literal[2]
    worker_instance_id: UUID


class OwnedBody(ResourceModel):
    protocol_version: Literal[2]
    ownership: AttemptOwnership


class StartBody(OwnedBody):
    operation_id: UUID
    host_report: SignedHostReport
    input_digest: str


class CompleteBody(OwnedBody):
    operation_id: UUID
    result: dict


class FailBody(OwnedBody):
    operation_id: UUID
    error: Annotated[str, Field(min_length=1)]
    failure_kind: Literal["error", "out_of_memory"] = "error"


class DeclineBody(OwnedBody):
    operation_id: UUID
    reason: Annotated[str, Field(min_length=1)]


class ReleaseBody(OwnedBody):
    operation_id: UUID
    host_report: SignedHostReport
    cleanup: SignedCleanup


class ProgressBody(OwnedBody):
    progress: dict


class AttemptState(ResourceModel):
    task_id: Positive
    attempt_id: UUID
    state: Literal["reserved", "running", "releasing", "recovering", "released"]
    task_status: int
    cancelled: bool
    server_time: datetime
    lease_expires_at: datetime
    execution_deadline: datetime | None
    progress: dict
