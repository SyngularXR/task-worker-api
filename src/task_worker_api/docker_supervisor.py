"""Trusted host-side attempt containers. Never mount this journal/key into workers.

Claims must come directly from the authenticated backend, not worker messages.
One container contains the entire attempt process tree; no Docker socket, host
PID namespace or privileges are granted. Ambiguous launches are reconciled, never
restarted. All methods are blocking and belong outside the worker event loop.
"""
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import time
from contextlib import closing
from datetime import datetime, timezone
from uuid import uuid4

from .host_reporter import physical_boot_id
from .resources import AdmissionError, CleanupEvidence
from .resource_protocol import CleanupObservation, SignedCleanup, observation_signature
from .service_protocol import ServiceGrant


class DockerSupervisor:
    def __init__(self, journal: Path, work_root: Path, *, authority_id, host_id, boot_id, epoch, execution_scope, cgroup_parent, network_id, observe_only=False):
        self.authority_id, self.host_id, self.boot_id, self.epoch = authority_id, host_id, boot_id, epoch
        if not re.fullmatch(r"[a-f0-9]{64}", network_id):
            raise ValueError("supervisor requires an inspected Docker network ID")
        self.network_id = network_id
        self.execution_scope, self.cgroup_parent = execution_scope, cgroup_parent
        self.journal = journal.resolve()
        self.root = work_root.resolve()
        if self.journal.is_relative_to(self.root):
            raise ValueError("supervisor journal must be outside attempt storage")
        if observe_only:
            if not self.root.is_dir():
                raise AdmissionError("launch_unknown")
            with closing(sqlite3.connect(self.journal.as_uri()+"?mode=ro",uri=True)) as db:
                db.execute("SELECT attempt,plan,phase,container,evidence FROM launches LIMIT 0")
            return
        self.root.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.journal)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS launches (attempt TEXT PRIMARY KEY, plan TEXT NOT NULL, phase TEXT NOT NULL, container TEXT, evidence TEXT)")

    def _docker(self, *args, timeout=30):
        return subprocess.run(["docker", *args], check=True, capture_output=True, text=True, timeout=timeout,
            **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {})).stdout.strip()

    def _binding(self, claim):
        if (claim.host_id != self.host_id or claim.boot_id != self.boot_id or claim.execution_scope != self.execution_scope
                or physical_boot_id(self.host_id, Path("/proc")) != self.boot_id):
            raise AdmissionError("attempt_fenced")
        if isinstance(claim, ServiceGrant) and (claim.authority_id != self.authority_id or claim.epoch != self.epoch):
            raise AdmissionError("attempt_fenced")

    def _row(self, claim):
        with closing(sqlite3.connect(self.journal)) as db:
            row = db.execute("SELECT plan,phase,container,evidence FROM launches WHERE attempt=?",
                             [str(claim.ownership.attempt_id)]).fetchone()
        if row is None:
            raise AdmissionError("launch_unknown")
        plan = json.loads(row[0])
        self._plan_binding(plan)
        if plan["ownership"] != claim.ownership.model_dump(mode="json"):
            raise AdmissionError("attempt_fenced")
        if isinstance(claim, ServiceGrant) and plan.get('service') != self._service_binding(claim):
            raise AdmissionError("attempt_fenced")
        return plan, *row[1:]

    def _plan_binding(self, plan):
        if (plan["root"] != str(self.root) or plan["host"] != str(self.host_id)
                or plan["boot"] != str(self.boot_id) or plan["scope"] != self.execution_scope
                or plan["cgroup_parent"] != self.cgroup_parent or plan["network_id"] != self.network_id
                or plan["authority"] != str(self.authority_id) or plan["epoch"] != self.epoch):
            raise AdmissionError("attempt_fenced")

    def verified_gpu_launch(self, owner, *, proc_root=Path("/proc"), profile_digest=None, inspection_timeout=30):
        """Readonly physical proof for a filtered authenticated authority owner."""
        from .borrowing_observer import process_identity
        from .resources import AttemptOwnership

        if physical_boot_id(self.host_id, proc_root) != self.boot_id or owner.execution_scope != self.execution_scope:
            raise AdmissionError("attempt_fenced")
        with closing(sqlite3.connect(self.journal.as_uri() + "?mode=ro", uri=True)) as db:
            row = db.execute("SELECT plan,phase,container FROM launches WHERE attempt=?", [str(owner.attempt_id)]).fetchone()
        if row is None:
            return None  # A reserved authority owner may not have launched yet.
        plan = json.loads(row[0])
        self._plan_binding(plan)
        ownership = AttemptOwnership.model_validate(plan["ownership"])
        if ((ownership.attempt_id, ownership.worker_instance_id, ownership.generation)
                != (owner.attempt_id, owner.worker_instance_id, owner.generation)):
            raise AdmissionError("attempt_fenced")
        service = plan.get("service")
        if owner.role == "inference":
            if (not service or service["engine_epoch"] != str(owner.engine_epoch)
                    or service["profile"]["service_id"] != owner.service_id
                    or service["profile"]["config_digest"] != profile_digest):
                raise AdmissionError("attempt_fenced")
        elif service is not None:
            raise AdmissionError("attempt_fenced")
        if row[1] in ("registered", "removed", "cleaned"):
            return None
        data = self._inspect(row[2] or plan["name"], plan, timeout=inspection_timeout)
        if not data["State"]["Running"]:
            return None
        if plan["gpu"] != owner.gpu_uuid:
            raise AdmissionError("attempt_fenced")
        pid = data["State"]["Pid"]
        start, cgroup = process_identity(pid, proc_root)
        if cgroup == "/":
            raise AdmissionError("launch_configuration_changed")
        return {"attempt_id": str(owner.attempt_id), "container_id": data["Id"],
                "pid": pid, "start_time": start, "cgroup": cgroup, "role": owner.role}

    def stop_verified_gpu_inference(self, owner, *, profile_digest, timeout, proc_root=Path("/proc")):
        """Withdraw only an exactly attributed inference container; never release accounting."""
        if owner.role != "inference":
            raise AdmissionError("service_grant_required")
        deadline = time.monotonic()+timeout
        proof = self.verified_gpu_launch(owner, proc_root=proc_root, profile_digest=profile_digest,inspection_timeout=timeout)
        if proof is None:
            return False
        self._docker("kill", "--signal=KILL", proof["container_id"], timeout=max(0,deadline-time.monotonic()))
        return True

    @staticmethod
    def _service_binding(grant):
        return {'engine_epoch':str(grant.engine_epoch), 'profile':grant.profile.model_dump(mode='json')}

    def launch_service(self, grant, lease, **kwargs):
        if not isinstance(grant, ServiceGrant) or lease.grant != grant:
            raise AdmissionError('service_grant_required')
        lease.require_live()
        self.register_service(grant)
        return self.launch(grant, **kwargs)

    def register_service(self, grant):
        """Journal fresh ownership before configuration/loading; absence is unknown."""
        self._binding(grant)
        if not isinstance(grant, ServiceGrant) or grant.state != 'warming':
            raise AdmissionError('service_grant_required')
        with closing(sqlite3.connect(self.journal)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT phase FROM launches WHERE attempt=?', [str(grant.ownership.attempt_id)]).fetchone()
            if row:
                self._row(grant)
                if row[0] != 'registered':
                    raise AdmissionError('launch_requires_reconciliation')
                return
            if (self.root/str(grant.ownership.attempt_id)).exists():
                raise AdmissionError('launch_requires_reconciliation')
            plan = dict(id=str(uuid4()), name='synpusher-attempt-'+grant.ownership.attempt_id.hex,
                ownership=grant.ownership.model_dump(mode='json'),
                root=str(self.root), scope=self.execution_scope, cgroup_parent=self.cgroup_parent,
                network_id=self.network_id, host=str(self.host_id), boot=str(self.boot_id),
                authority=str(self.authority_id), epoch=self.epoch, service=self._service_binding(grant))
            db.execute("INSERT INTO launches VALUES(?,?,'registered',NULL,NULL)",
                       [str(grant.ownership.attempt_id),json.dumps(plan)])

    def running_service(self, grant):
        return self.running(grant)

    def service_address(self, grant):
        self._binding(grant)
        plan, phase, container, _ = self._row(grant)
        if phase != 'started':
            raise AdmissionError('service_not_running')
        data = self._inspect(container, plan)
        if not data['State']['Running']:
            raise AdmissionError('service_not_running')
        networks = data['NetworkSettings']['Networks']
        if len(networks) != 1:
            raise AdmissionError('launch_configuration_changed')
        return str(ipaddress.ip_address(next(iter(networks.values()))['IPAddress']))

    def cleanup_service(self, grant):
        self._binding(grant)
        plan, phase, _, _ = self._row(grant)
        if phase == 'registered':
            if (self.root/str(grant.ownership.attempt_id)).exists():
                raise AdmissionError('cleanup_unverified')
            proof = CleanupEvidence(attempt_id=grant.ownership.attempt_id,boot_id=self.boot_id,
                processes_stopped=True,models_evicted=True,scratch_cleaned=True,
                evidence_id=hashlib.sha256((plan['id']+'not_launched').encode()).hexdigest())
            self._record(grant,'cleaned',None,proof.model_dump_json())
            return proof
        return self.cleanup(grant)

    def sign_cleanup_service(self, grant, key):
        proof = self.cleanup_service(grant)
        observation = CleanupObservation(host_id=self.host_id,issued_at=datetime.now(timezone.utc),evidence=proof)
        return SignedCleanup(authority_id=self.authority_id,epoch=self.epoch,observation=observation,
            signature=observation_signature('cleanup',self.authority_id,self.epoch,observation.model_dump(mode='json'),key))

    def force_stop_service(self, grant):
        """Kill only the exact owned container; failures retain unknown ownership."""
        self._binding(grant)
        plan, phase, container, _ = self._row(grant)
        if phase not in ('registered', 'removed', 'cleaned'):
            data = self._inspect(container or plan['name'], plan)
            if data['State']['Running']:
                self._docker('kill', '--signal=KILL', data['Id'], timeout=grant.profile.reclaim_timeout_seconds)
        return self.cleanup_service(grant)

    def _record(self, claim, phase, container, evidence=None):
        with closing(sqlite3.connect(self.journal)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT phase FROM launches WHERE attempt=?", [str(claim.ownership.attempt_id)]).fetchone()
            allowed = {"registered": {"cleaned"}, "creating": {"starting", "removing"}, "starting": {"started", "removing"},
                       "started": {"removing"}, "removing": {"removed"}, "removed": {"cleaned"}, "cleaned": set()}
            if old is None or (phase != old[0] and phase not in allowed[old[0]]):
                raise AdmissionError("launch_requires_reconciliation")
            db.execute("UPDATE launches SET phase=?,container=?,evidence=COALESCE(?,evidence) WHERE attempt=?",
                       [phase, container, evidence, str(claim.ownership.attempt_id)])

    def _inspect(self, name, plan, *, timeout=30):
        data = json.loads(self._docker("inspect", name,timeout=timeout))[0]
        labels = data["Config"]["Labels"] or {}
        config = data["HostConfig"]
        networks = data.get("NetworkSettings", {}).get("Networks", {})
        requests = config.get("DeviceRequests") or []
        expected_gpu = [plan["gpu"]] if plan["gpu"] else []
        devices = [device for request in requests for device in (request.get("DeviceIDs") or [])]
        if plan["environment"].get("NVIDIA_DRIVER_CAPABILITIES") == "graphics,utility":
            if plan["environment"].get("SPECTRA_GPU_BACKEND") == "vulkan" and config.get("Runtime") != "nvidia":
                raise AdmissionError("launch_configuration_changed")
            for name in ("NVIDIA_DRIVER_CAPABILITIES", "SPECTRA_GPU_BACKEND", "SPECTRA_GPU_ID"):
                if name in plan["environment"]:
                    values = [value for value in data["Config"].get("Env", []) if value.startswith(name + "=")]
                    if values != [name + "=" + plan["environment"][name]]:
                        raise AdmissionError("launch_configuration_changed")
        if (labels.get("synpusher.launch") != plan["id"] or data["Image"] != plan["image"]
                or data["Config"]["User"] != plan["user"]
                or config["Privileged"] or config["PidMode"] or config.get("CapAdd") or config.get("Devices")
                or config["RestartPolicy"]["Name"] != "no" or devices != expected_gpu
                or config["CgroupParent"] != plan["cgroup_parent"]
                or config.get("NetworkMode") != plan["network_id"] or len(networks) != 1
                or any(network.get("NetworkID") != plan["network_id"] for network in networks.values()
                       if data.get("State", {}).get("Running") or network.get("NetworkID"))
                or len(requests) != bool(plan["gpu"]) or any(request["Count"] != 0 for request in requests)
                or not config["ReadonlyRootfs"] or "ALL" not in config["CapDrop"]
                or "no-new-privileges" not in config["SecurityOpt"]
                or config["Memory"] != plan["memory"] * 1024 * 1024 or config["NanoCpus"] != plan["cpu"] * 1_000_000):
            raise AdmissionError("launch_configuration_changed")
        if plan.get("publication"):
            mounts = [mount for mount in data.get("Mounts", []) if mount["Destination"] == "/app/shared"]
            if (len(mounts) != 1 or mounts[0]["Type"] != "bind" or not mounts[0]["RW"]
                    or Path(mounts[0]["Source"]).resolve() != Path(plan["publication"])):
                raise AdmissionError("launch_configuration_changed")
        return data

    def running(self, claim):
        plan, phase, container, _ = self._row(claim)
        if phase in ("registered", "removed", "cleaned"):
            return False
        return self._inspect(container or plan["name"], plan)["State"]["Running"]

    def _record_refusal(self, claim):
        if claim.state == "running":
            return
        plan = dict(id=str(uuid4()), name="synpusher-attempt-" + claim.ownership.attempt_id.hex,
            ownership=claim.ownership.model_dump(mode="json"), root=str(self.root),
            host=str(self.host_id), boot=str(self.boot_id), scope=self.execution_scope,
            cgroup_parent=self.cgroup_parent, network_id=self.network_id,
            authority=str(self.authority_id), epoch=self.epoch)
        with closing(sqlite3.connect(self.journal)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            # A refusal cannot replace an existing, possibly live launch.
            db.execute("INSERT INTO launches VALUES(?,?,'removed',NULL,NULL) ON CONFLICT(attempt) DO NOTHING",
                       [str(claim.ownership.attempt_id), json.dumps(plan)])

    def launch(self, claim, *, image: str, command: list[str], read_only_mounts: dict[str, str], journal_directory: Path | None = None, environment: dict[str, str] | None = None, publication_directory: Path | None = None):
        self._binding(claim)
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", image) or not command:
            raise ValueError("launch requires an inspected image ID and command")
        service = isinstance(claim, ServiceGrant)
        if claim.state != ('warming' if service else 'reserved'):
            if not service:
                self._record_refusal(claim)
            raise AdmissionError("launch_requires_reserved_attempt")
        if claim.profile.gpu_backend == "dx12":
            if not service:
                self._record_refusal(claim)
            raise AdmissionError("unsupported_gpu_backend")
        attempt = str(claim.ownership.attempt_id)
        # Numeric host UIDs need not exist in the image's passwd database.
        # Python/getpass and ML caches still need a name and writable home.
        environment = {"HOME": "/work", "USER": "worker", "LOGNAME": "worker",
                       "XDG_CACHE_HOME": "/work/.cache", **(environment or {})}
        for name, value in environment.items():
            if (not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or not isinstance(value, str)
                    or name in ("CUDA_VISIBLE_DEVICES", "NVIDIA_VISIBLE_DEVICES", "SYNPUSHER_TARGETS",
                                "SPECTRA_GPU_BACKEND", "SPECTRA_GPU_ID")):
                raise ValueError("invalid or supervisor-owned environment variable")
        if claim.profile.gpu_backend == "vulkan":
            if "NVIDIA_DRIVER_CAPABILITIES" in environment:
                raise ValueError("Vulkan driver capabilities are supervisor-owned")
            environment["NVIDIA_DRIVER_CAPABILITIES"] = "graphics,utility"
            environment["SPECTRA_GPU_BACKEND"] = "vulkan"
            environment["SPECTRA_GPU_ID"] = claim.gpu_uuid
        directory = self.root / attempt
        if publication_directory is not None:
            publication_directory = publication_directory.resolve(strict=True)
            if (service or claim.gpu_uuid is not None or claim.profile.gpu_count != 0
                    or claim.task.task_type not in ("finalize_spatial", "finalize_gs", "finalize_render", "finalize_segment", "finalize_synthetic", "finalize_gs4d", "finalize_model", "finalize_cinematic", "finalize_deploy", "finalize_deploy_prep")
                    or command != ["python", "-m", "src.services.resource_finalizer_worker", "/run/worker-config.json"]):
                raise ValueError("publication mount requires the backend CPU finalizer")
            if (not publication_directory.is_dir() or publication_directory == Path(publication_directory.anchor)
                    or self.journal.is_relative_to(publication_directory)
                    or self.root.is_relative_to(publication_directory) or publication_directory.is_relative_to(self.root)):
                raise ValueError("unsafe publication mount")
        if journal_directory is not None:
            journal_directory = journal_directory.resolve(strict=True)
            if self.journal.is_relative_to(journal_directory) or journal_directory.is_relative_to(self.root):
                raise ValueError("worker journal mount must exclude supervisor state and attempt scratch")
        # Compute gets only attempt scratch and its claim journal writable.
        # The dedicated backend finalizer may also write its publication store.
        for source, target in read_only_mounts.items():
            resolved = Path(source).resolve(strict=True)
            if (self.journal == resolved or self.journal.is_relative_to(resolved)
                    or resolved.name in ("docker.sock", "docker_engine") or not target.startswith("/")
                    or target == "/" or target == "/work" or target.startswith("/work/")):
                raise ValueError("unsafe worker mount")
            if publication_directory is not None and (target == "/app" or target == "/app/shared" or target.startswith("/app/shared/")):
                raise ValueError("publication mount overlaps a read-only mount")
        deadline = min(claim.lease_expires_at, claim.residence_deadline) if service else claim.staging_deadline
        remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            if not service:
                self._record_refusal(claim)
            raise AdmissionError("staging_deadline_exceeded")
        plan = dict(id=str(uuid4()), name="synpusher-attempt-" + claim.ownership.attempt_id.hex,
            ownership=claim.ownership.model_dump(mode="json"), image=image, command=command,
            user=f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") else "1000:1000",
            gpu=claim.gpu_uuid, memory=claim.profile.execution_ram_mib, cpu=claim.profile.cpu_millicores,
            mounts=read_only_mounts, environment=environment, publication=str(publication_directory) if publication_directory else None,
            root=str(self.root), scope=self.execution_scope, cgroup_parent=self.cgroup_parent, network_id=self.network_id,
            host=str(self.host_id), boot=str(self.boot_id), authority=str(self.authority_id), epoch=self.epoch)
        if service:
            plan['service'] = self._service_binding(claim)
        with closing(sqlite3.connect(self.journal)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT phase FROM launches WHERE attempt=?", [attempt]).fetchone()
            if old:
                if not service or old[0] != 'registered':
                    raise AdmissionError("launch_requires_reconciliation")
                self._row(claim)
                db.execute("UPDATE launches SET plan=?,phase='creating' WHERE attempt=?", [json.dumps(plan),attempt])
            else:
                db.execute("INSERT INTO launches VALUES(?,?,'creating',NULL,NULL)", [attempt, json.dumps(plan)])
        directory.mkdir(exist_ok=False)
        args = ["create", "--network", plan["network_id"], "--name", plan["name"], "--label", "synpusher.launch=" + plan["id"],
                # Match host-owned scratch and journal permissions without capabilities.
                "--user", plan["user"],
                "--restart=no", "--init", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                "--read-only", "--memory", f"{plan['memory']}m", "--memory-swap", f"{plan['memory']}m",
                "--cpus", str(plan["cpu"] / 1000), "--pids-limit", "512", "--tmpfs", "/tmp:rw,size=256m",
                "--mount", f"type=bind,source={directory},target=/work"]
        if plan["gpu"]:
            args += ["--gpus", "device=" + plan["gpu"]]
            if claim.profile.gpu_backend == "vulkan":
                args += ["--runtime=nvidia"]
        else:
            args += ["--runtime=runc", "--env=NVIDIA_VISIBLE_DEVICES=void", "--env=CUDA_VISIBLE_DEVICES=-1"]
        if self.cgroup_parent:
            args += ["--cgroup-parent", self.cgroup_parent]
        for name, value in environment.items():
            args += ["--env", name + "=" + value]
        if journal_directory is not None:
            args += ["--mount", f"type=bind,source={journal_directory},target=/run/worker-journal"]
        if publication_directory is not None:
            args += ["--mount", f"type=bind,source={publication_directory},target=/app/shared"]
        for source, target in read_only_mounts.items():
            args += ["--mount", f"type=bind,source={Path(source).resolve()},target={target},readonly"]
        container = self._docker(*args, "--entrypoint", command[0], image, *command[1:], timeout=remaining)
        data = self._inspect(container, plan)
        container = data["Id"]
        # Commit before start: an unknown start outcome must never issue start again.
        self._record(claim, "starting", container)
        self._docker("start", container)
        self._record(claim, "started", container)
        return container

    def cleanup(self, claim):
        self._binding(claim)
        plan, phase, container, evidence = self._row(claim)
        if phase == "cleaned":
            return CleanupEvidence.model_validate_json(evidence)
        out_of_memory = bool(evidence and CleanupEvidence.model_validate_json(evidence).out_of_memory)
        if phase == "removing" and not self._docker("container", "ls", "--all", "--no-trunc", "--filter", "id=" + container, "--format", "{{.ID}}"):
            # Successful engine enumeration proves the exact ID is gone after
            # an ambiguous rm response. An inspect/transport error proves nothing.
            self._record(claim, "removed", container)
            phase = "removed"
        if phase != "removed":
            # Missing/failed inspect is unknown, never process-exit evidence.
            data = self._inspect(container or plan["name"], plan)
            container = data["Id"]
            if data["State"]["Running"]:
                self._docker("stop", "--time", "10", container)
                data = self._inspect(container, plan)
            if data["State"]["Running"] or data["State"]["Pid"] != 0:
                raise AdmissionError("cleanup_unverified")
            out_of_memory = out_of_memory or data["State"]["OOMKilled"]
            partial = CleanupEvidence(attempt_id=claim.ownership.attempt_id, boot_id=self.boot_id,
                processes_stopped=True, models_evicted=True, scratch_cleaned=False,
                evidence_id=hashlib.sha256((plan["id"] + container).encode()).hexdigest(),
                out_of_memory=out_of_memory)
            self._record(claim, "removing", container, partial.model_dump_json())
            # Removing the exact stopped ID fences any delayed start request.
            # No code ever reissues create/start for an existing launch intent.
            self._docker("rm", "--volumes", container)
            self._record(claim, "removed", container)
        directory = (self.root / str(claim.ownership.attempt_id)).resolve()
        if directory.parent != self.root:
            raise AdmissionError("unsafe_cleanup_path")
        if directory.exists():
            shutil.rmtree(directory)
        proof = CleanupEvidence(attempt_id=claim.ownership.attempt_id, boot_id=self.boot_id,
            processes_stopped=True, models_evicted=True, scratch_cleaned=True,
            out_of_memory=out_of_memory,
            evidence_id=hashlib.sha256((plan["id"] + (container or "")).encode()).hexdigest())
        self._record(claim, "cleaned", container, proof.model_dump_json())
        return proof

    def sign_cleanup(self, claim, key: bytes):
        proof = self.cleanup(claim)
        observation = CleanupObservation(host_id=self.host_id, issued_at=datetime.now(timezone.utc), evidence=proof)
        return SignedCleanup(authority_id=self.authority_id, epoch=self.epoch, observation=observation,
            signature=observation_signature("cleanup", self.authority_id, self.epoch, observation.model_dump(mode="json"), key))
