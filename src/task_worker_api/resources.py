"""Protocol-v2 resource contracts; quantities are MiB and CPU millicores.

No production budgets live in the SDK. Profiles are resolved by the backend.
These contracts do not enable the v2 worker loop by themselves.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_serializer, model_validator

NonNegative = Annotated[StrictInt, Field(ge=0)]
Positive = Annotated[StrictInt, Field(gt=0)]
Identifier = Annotated[str, Field(min_length=1, max_length=255)]


class ResourceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ResourceProfile(ResourceModel):
    profile_id: Identifier
    revision: Positive
    task_type: Identifier
    required_capabilities: frozenset[str]
    workload_bounds: dict[str, NonNegative]
    gpu_count: Annotated[StrictInt, Field(ge=0, le=1)]
    gpu_backend: Literal["cuda", "vulkan", "dx12", "none"]
    gpu_vram_mib: NonNegative
    host_ram_mib: Positive
    execution_ram_mib: Positive
    cpu_millicores: Positive
    scratch_mib: NonNegative
    scratch_pool: Identifier
    execution_timeout_seconds: Positive
    staging_timeout_seconds: Annotated[StrictInt, Field(ge=60, le=900)]
    evidence: Annotated[str, Field(min_length=1)]
    validation_state: Literal["unvalidated", "validated"]

    @field_serializer("required_capabilities")
    def sorted_capabilities(self, value):
        return sorted(value)

    @model_validator(mode="after")
    def coherent_resources(self):
        if self.gpu_count == 0:
            if self.gpu_backend != "none" or self.gpu_vram_mib != 0:
                raise ValueError("CPU profile must explicitly request no GPU")
        elif self.gpu_backend == "none" or self.gpu_vram_mib == 0:
            raise ValueError("GPU profile requires a GPU backend and positive VRAM budget")
        if self.execution_ram_mib > self.host_ram_mib:
            raise ValueError("execution RAM cannot exceed total host RAM request")
        return self


class Capacity(ResourceModel):
    """Already excludes configured baseline/headroom; never subtract them twice."""

    allocatable: NonNegative
    available: NonNegative

    @model_validator(mode="after")
    def consistent(self):
        if self.available > self.allocatable:
            raise ValueError("available capacity exceeds allocatable capacity")
        return self


class ExecutionCapacity(ResourceModel):
    ram: Capacity
    cpu_millicores: NonNegative
    parent: Identifier | None = None


class HostSnapshot(ResourceModel):
    """Persist only after reporter authentication; worker input is not trusted."""

    host_id: UUID
    boot_id: UUID
    sequence: Positive
    captured_at: datetime
    host_ram: Capacity
    cpu_millicores: NonNegative
    execution_scopes: dict[str, ExecutionCapacity]
    scratch_pools: dict[str, Capacity]
    gpus: dict[str, Capacity]

    @model_validator(mode="after")
    def aware_time(self):
        if self.captured_at.utcoffset() is None:
            raise ValueError("snapshot capture time requires a timezone")
        if any(not gpu.startswith("GPU-") for gpu in self.gpus):
            raise ValueError("physical GPU UUID required, not device index")
        for scope in self.execution_scopes:
            seen = set()
            current = scope
            while current is not None:
                if current not in self.execution_scopes:
                    raise ValueError("execution scope parent is missing")
                if current in seen:
                    raise ValueError("execution scope hierarchy contains a cycle")
                seen.add(current)
                current = self.execution_scopes[current].parent
        return self


class ClaimRequest(ResourceModel):
    protocol_version: Literal[2]
    worker_instance_id: UUID
    claim_request_id: UUID
    task_types: frozenset[Identifier]

    @field_serializer("task_types")
    def sorted_types(self, value):
        return sorted(value)

    @model_validator(mode="after")
    def nonempty_types(self):
        if not self.task_types:
            raise ValueError("at least one task type required")
        return self


class AttemptOwnership(ResourceModel):
    worker_instance_id: UUID
    attempt_id: UUID
    generation: Positive
    token: Annotated[str, Field(min_length=32, repr=False)]


class InputArtifact(ResourceModel):
    filename: Identifier
    path: Identifier
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    size_bytes: NonNegative

    @model_validator(mode="after")
    def safe_paths(self):
        from .files import _require_safe_filename
        from .errors import ProtocolError

        try:
            for component in [self.filename, *self.path.split("/")]:
                _require_safe_filename(component, field="input", key="path")
        except ProtocolError as exc:
            raise ValueError("input paths must contain safe relative components") from exc
        return self


class AdmittedTask(ResourceModel):
    """Backend-owned task input frozen when its resource profile is resolved."""

    id: Positive
    task_type: Identifier
    case_id: Positive | None
    item_key: str
    params: dict
    inputs: dict[Identifier, InputArtifact]


def input_snapshot_digest(task: AdmittedTask) -> str:
    import hashlib
    import json

    return hashlib.sha256(json.dumps(task.model_dump(mode="json"), sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class ClaimResult(ResourceModel):
    task_id: Positive
    task: AdmittedTask
    ownership: AttemptOwnership
    profile: ResourceProfile
    input_digest: Identifier
    gpu_uuid: str | None
    host_id: UUID
    boot_id: UUID
    execution_scope: Identifier
    staging_deadline: datetime
    lease_expires_at: datetime
    state: Literal["reserved", "running", "releasing", "recovering", "released"]

    @model_validator(mode="after")
    def task_matches_reservation(self):
        if self.task.id != self.task_id or self.task.task_type != self.profile.task_type:
            raise ValueError("task payload differs from resource reservation")
        return self


class CleanupEvidence(ResourceModel):
    """Construct in the trusted supervisor adapter, never from worker booleans."""

    attempt_id: UUID
    boot_id: UUID
    processes_stopped: bool
    models_evicted: bool
    scratch_cleaned: bool
    evidence_id: Identifier
    out_of_memory: bool = False


def is_out_of_memory(error: Exception) -> bool:
    """Recognize allocation failures without importing a GPU runtime."""
    return (isinstance(error, MemoryError)
            or any(cls.__name__ == "OutOfMemoryError" for cls in type(error).__mro__)
            or "cuda out of memory" in str(error).lower())


class AdmissionError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)
