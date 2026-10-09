"""Borrowing-only observations; the primary hardware report contract is unchanged."""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from .errors import ProtocolError
from .resource_protocol import Signature, observation_signature
from .resources import Identifier, NonNegative, Positive, ResourceModel

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
PhysicalGPU = Annotated[str, Field(pattern=r"^GPU-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")]


def policy_digest(policy: dict) -> str:
    return hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class BorrowingPolicy(ResourceModel):
    driver_version: Identifier
    collection_timeout_seconds: Positive
    observation_interval_seconds: Positive
    reclaim_timeout_seconds: Positive
    max_processes: Positive
    max_owners: Positive
    max_output_bytes: Positive


class BorrowingBinding(ResourceModel):
    authority_id: UUID
    host_id: UUID
    boot_id: UUID
    epoch: Positive
    service_id: Identifier
    worker_instance_id: UUID
    gpu_uuid: PhysicalGPU
    policy_digest: Digest
    profile_digest: Digest


class BorrowingOwner(ResourceModel):
    attempt_id: UUID
    worker_instance_id: UUID
    generation: Positive
    execution_scope: Identifier
    gpu_uuid: PhysicalGPU
    state: Literal["reserved", "running", "releasing", "recovering"]
    lease_expires_at: AwareDatetime
    role: Literal["primary", "inference"]
    service_id: Identifier | None
    engine_epoch: UUID | None

    @model_validator(mode="after")
    def coherent_role(self):
        if (self.role == "inference") != (self.service_id is not None and self.engine_epoch is not None):
            raise ValueError("owner role differs from service binding")
        if self.role == "primary" and (self.service_id is not None or self.engine_epoch is not None):
            raise ValueError("primary owner has service fields")
        return self


class BorrowingOwners(ResourceModel):
    binding: BorrowingBinding
    report_sequence: Positive
    report_captured_at: AwareDatetime
    server_time: AwareDatetime
    disabled: bool
    complete: bool
    owners: tuple[BorrowingOwner, ...]

    @model_validator(mode="after")
    def coherent_owners(self):
        if any(owner.gpu_uuid != self.binding.gpu_uuid for owner in self.owners):
            raise ValueError("owner GPU differs from borrowing binding")
        if len({owner.attempt_id for owner in self.owners}) != len(self.owners):
            raise ValueError("duplicate owner")
        return self


class BorrowingIncident(ResourceModel):
    binding: BorrowingBinding
    incident_id: UUID
    hold_generation: Positive
    observed_sequence: Positive
    observed_at: AwareDatetime
    reason: Literal["untracked_owner", "ownership_unverified", "outside_fence"]
    evidence_digest: Digest


class BorrowingFault(ResourceModel):
    version: Literal[1]
    binding: BorrowingBinding
    incident: BorrowingIncident
    delivery_sequence: Positive
    delivery_at: AwareDatetime

    @model_validator(mode="after")
    def ordered_observation(self):
        if (self.binding.host_id, self.binding.gpu_uuid) != (self.incident.binding.host_id, self.incident.binding.gpu_uuid):
            raise ValueError("fault delivery differs from original host GPU")
        if ((self.binding == self.incident.binding and self.delivery_sequence < self.incident.observed_sequence)
                or self.delivery_at < self.incident.observed_at):
            raise ValueError("fault delivery precedes observation")
        return self


class BorrowingAck(ResourceModel):
    incident: BorrowingIncident
    recorded_at: AwareDatetime
    disabled: Literal[True]


class BorrowingClear(ResourceModel):
    binding: BorrowingBinding
    clear_id: UUID
    previous_binding_digest: Digest
    hold_generation: Positive
    incidents: dict[UUID, Digest]
    inspection_digest: Digest
    report_sequence: Positive
    recorded_at: AwareDatetime
    response_at: AwareDatetime

    @model_validator(mode="after")
    def real_incident_clear(self):
        if not self.incidents:
            raise ValueError("clear requires real retained incidents")
        return self


class SignedBorrowingOwners(ResourceModel):
    observation: BorrowingOwners
    signature: Signature


class SignedBorrowingFault(ResourceModel):
    observation: BorrowingFault
    signature: Signature


class SignedBorrowingAck(ResourceModel):
    observation: BorrowingAck
    signature: Signature


class SignedBorrowingClear(ResourceModel):
    observation: BorrowingClear
    signature: Signature


class BorrowingInspection(ResourceModel):
    binding: BorrowingBinding
    previous_binding_digest: Digest
    hold_generation: Positive
    incidents: dict[UUID, Digest]
    owners: SignedBorrowingOwners
    inspected_at: AwareDatetime
    inspection_digest: Digest
    idle: Literal[True]

    @model_validator(mode="after")
    def exact_inspection(self):
        if self.owners.observation.binding != self.binding or not self.incidents:
            raise ValueError("inspection requires matching owner view and real incidents")
        return self


class SignedBorrowingInspection(ResourceModel):
    observation: BorrowingInspection
    signature: Signature


DOMAINS = {
    BorrowingInspection: ("borrowing_inspection", SignedBorrowingInspection),
    BorrowingOwners: ("borrowing_owners", SignedBorrowingOwners),
    BorrowingFault: ("borrowing_fault", SignedBorrowingFault),
    BorrowingAck: ("borrowing_ack", SignedBorrowingAck),
    BorrowingClear: ("borrowing_clear", SignedBorrowingClear),
}


def sign_borrowing(observation, key: bytes):
    domain, wrapper = DOMAINS[type(observation)]
    binding = observation.incident.binding if isinstance(observation, BorrowingAck) else observation.binding
    return wrapper(observation=observation, signature=observation_signature(
        domain, binding.authority_id, binding.epoch, observation.model_dump(mode="json"), key))


def verify_borrowing(signed, key: bytes, binding: BorrowingBinding):
    observation = signed.observation
    actual = observation.incident.binding if isinstance(observation, BorrowingAck) else observation.binding
    if actual != binding or not hmac.compare_digest(sign_borrowing(observation, key).signature, signed.signature):
        raise ProtocolError("borrowing observation is fenced or unauthenticated")
    return observation


def fresh_observation(now: datetime, captured: datetime) -> bool:
    return -5 <= (now - captured).total_seconds() <= 15
