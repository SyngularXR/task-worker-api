"""Resident hardware ownership, independent of task execution and round outcomes."""
import hmac
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_serializer, model_validator

from .errors import ProtocolError
from .resources import AttemptOwnership, Identifier, Positive, ResourceBudget, ResourceModel
from .resource_protocol import OwnedBody, Signature, SignedCleanup, SignedHostReport, observation_signature

ServicePhase = Literal['warming', 'idle', 'serving', 'revoked', 'expired', 'releasing', 'released']
Digest = Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]


class ServiceProfile(ResourceBudget):
    profile_id: Identifier
    revision: Positive
    service_id: Identifier
    model_id: Identifier
    model_revision: Identifier
    config_digest: Digest
    destination: Identifier
    data_scope: Literal['public_synthetic']
    max_residence_seconds: Positive
    reclaim_timeout_seconds: Positive
    evidence: Annotated[str, Field(min_length=1)]
    validation_state: Literal['unvalidated', 'validated']
    required_capabilities: frozenset[Identifier] = frozenset()

    @field_serializer('required_capabilities')
    def sorted_capabilities(self, value):
        return sorted(value)

    @model_validator(mode='after')
    def bounded_reclaim(self):
        if self.reclaim_timeout_seconds > self.max_residence_seconds:
            raise ValueError('reclaim bound exceeds residence budget')
        return self


class ServiceState(ResourceModel):
    attempt_id: UUID
    worker_instance_id: UUID
    generation: Positive
    authority_id: UUID
    host_id: UUID
    boot_id: UUID
    epoch: Positive
    engine_epoch: UUID
    server_time: AwareDatetime
    lease_expires_at: AwareDatetime
    residence_deadline: AwareDatetime
    state: ServicePhase

    @model_validator(mode='after')
    def finite_lease(self):
        if self.lease_expires_at > self.residence_deadline:
            raise ValueError('lease exceeds residence deadline')
        return self


class ServiceGrant(ResourceModel):
    ownership: AttemptOwnership
    profile: ServiceProfile
    authority_id: UUID
    host_id: UUID
    boot_id: UUID
    epoch: Positive
    engine_epoch: UUID
    execution_scope: Identifier
    gpu_uuid: str | None
    server_time: AwareDatetime
    lease_expires_at: AwareDatetime
    residence_deadline: AwareDatetime
    state: ServicePhase

    @model_validator(mode='after')
    def coherent_grant(self):
        if self.profile.validation_state != 'validated':
            raise ValueError('service profile is not validated')
        if bool(self.gpu_uuid) != bool(self.profile.gpu_count):
            raise ValueError('service GPU differs from resource profile')
        if self.gpu_uuid is not None:
            if not self.gpu_uuid.startswith('GPU-'):
                raise ValueError('physical GPU UUID required')
            UUID(self.gpu_uuid[4:])
        if self.lease_expires_at > self.residence_deadline:
            raise ValueError('lease exceeds residence deadline')
        if self.state in ('warming', 'idle', 'serving'):
            residence = (self.residence_deadline-self.server_time).total_seconds()
            if self.lease_expires_at <= self.server_time or not 0 < residence <= self.profile.max_residence_seconds:
                raise ValueError('live service exceeds its finite profile deadlines')
        return self


class SignedServiceGrant(ResourceModel):
    authority_id: UUID
    epoch: Positive
    grant: ServiceGrant
    signature: Signature

    @model_validator(mode='after')
    def authority_matches(self):
        if (self.authority_id, self.epoch) != (self.grant.authority_id, self.grant.epoch):
            raise ValueError('grant signing authority differs from ownership')
        return self


def sign_service_grant(grant: ServiceGrant, key: bytes) -> SignedServiceGrant:
    return SignedServiceGrant(authority_id=grant.authority_id, epoch=grant.epoch, grant=grant,
        signature=observation_signature('service_grant', grant.authority_id, grant.epoch,
                                        grant.model_dump(mode='json'), key))


def verify_service_grant(signed: SignedServiceGrant, key: bytes) -> ServiceGrant:
    expected = sign_service_grant(signed.grant, key).signature
    if not hmac.compare_digest(expected, signed.signature):
        raise ProtocolError('service grant signature differs')
    return signed.grant


class ServiceClaimBody(ResourceModel):
    protocol_version: Literal[2]
    worker_instance_id: UUID
    request_id: UUID
    service_id: Identifier
    host_report: SignedHostReport


class ServiceReadyBody(OwnedBody):
    operation_id: UUID
    host_report: SignedHostReport


class ServiceRenewBody(ServiceReadyBody):
    pass


class ServiceReleaseBody(ServiceReadyBody):
    cleanup: SignedCleanup


def validate_service_state(grant: ServiceGrant, state: ServiceState) -> ServiceState:
    ownership = grant.ownership
    expected = (ownership.attempt_id, ownership.worker_instance_id, ownership.generation,
                grant.authority_id, grant.host_id, grant.boot_id, grant.epoch, grant.engine_epoch,
                grant.residence_deadline)
    actual = (state.attempt_id, state.worker_instance_id, state.generation, state.authority_id,
              state.host_id, state.boot_id, state.epoch, state.engine_epoch, state.residence_deadline)
    if actual != expected:
        raise ProtocolError('service response belongs to another ownership epoch')
    return state
