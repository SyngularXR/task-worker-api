"""Trusted Linux GPU ownership collector; no task payloads or model credentials."""
import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import subprocess
import tempfile
import stat
import xml.etree.ElementTree as ET

from .borrowing_journal import BorrowingJournal
from .borrowing_protocol import (
    BorrowingBinding, BorrowingFault, BorrowingPolicy, SignedBorrowingAck,
    SignedBorrowingOwners, fresh_observation, policy_digest, sign_borrowing, verify_borrowing,
)
from .errors import ProtocolError


class UntrackedGPUProcess(ProtocolError):
    pass


class OutsideGPUFence(ProtocolError):
    pass


def process_identity(pid, proc_root=Path("/proc")):
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise ProtocolError("invalid physical process identity")
    root = proc_root / str(pid)
    first = (root / "stat").read_text(encoding="utf-8")
    start = int(first.rsplit(") ", 1)[1].split()[19])
    entries = (root / "cgroup").read_text(encoding="utf-8").splitlines()
    if len(entries) != 1 or not entries[0].startswith("0::/"):
        raise ProtocolError("unsupported process cgroup attribution")
    cgroup = entries[0][3:]
    if not cgroup.startswith("/") or "/../" in cgroup or cgroup.endswith("/.."):
        raise ProtocolError("invalid process cgroup attribution")
    again = (root / "stat").read_text(encoding="utf-8")
    if int(again.rsplit(") ", 1)[1].split()[19]) != start:
        raise ProtocolError("physical PID was reused")
    return start, cgroup


def nvidia_processes(raw, binding, policy):
    if len(raw) > policy.max_output_bytes:
        raise ProtocolError("GPU inventory exceeded qualified bound")
    root = ET.fromstring(raw)
    if root.findtext("driver_version") != policy.driver_version:
        raise ProtocolError("GPU driver differs from qualified version")
    matches = [gpu for gpu in root.findall("gpu") if gpu.findtext("uuid") == binding.gpu_uuid]
    if len(matches) != 1:
        raise ProtocolError("physical GPU inventory differs")
    gpu = matches[0]
    mig = gpu.findtext("mig_mode/current_mig")
    if mig not in ("Disabled", "N/A"):
        raise ProtocolError("MIG ownership is unqualified")
    processes = gpu.find("processes")
    if processes is None or (processes.text or "").strip() == "N/A":
        raise ProtocolError("GPU process coverage is unavailable")
    result = []
    for process in processes.findall("process_info"):
        kind = process.findtext("type")
        if kind not in {"C", "G", "C+G", "M", "O", "M+C"}:
            raise ProtocolError("GPU process class is unqualified")
        if kind in {"M", "M+C"}:
            raise ProtocolError("MPS client ownership is unqualified")
        pid = int(process.findtext("pid"))
        if pid <= 0:
            raise ProtocolError("invalid GPU PID")
        result.append((pid, kind))
    if len(result) > policy.max_processes or len(set(result)) != len(result):
        raise ProtocolError("GPU process inventory is incomplete")
    return result


def collect_nvidia(binding, policy):
    if os.name != "posix":
        raise ProtocolError("native borrowing attribution is unqualified")
    with tempfile.TemporaryFile() as output:
        subprocess.run(["nvidia-smi", "-q", "-x", "-i", binding.gpu_uuid],
            stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL,
            check=True, timeout=policy.collection_timeout_seconds)
        output.seek(0)
        return output.read(policy.max_output_bytes + 1)


def inspect_gpu(view, policy, supervisors, *, proc_root=Path("/proc"), raw=None):
    from .host_reporter import physical_boot_id

    binding = view.binding
    if physical_boot_id(binding.host_id, proc_root) != binding.boot_id:
        raise ProtocolError("physical host boot changed")
    if policy_digest(policy.model_dump(mode="json")) != binding.policy_digest or len(view.owners) > policy.max_owners:
        raise ProtocolError("borrowing policy differs or owner inventory exceeds bound")
    processes = nvidia_processes(raw if raw is not None else collect_nvidia(binding, policy), binding, policy)
    proofs = []
    primary = any(owner.role == "primary" for owner in view.owners)
    for owner in view.owners:
        matching = [supervisor for worker,supervisor in supervisors.items() if worker == owner.worker_instance_id and supervisor.execution_scope == owner.execution_scope]
        if len(matching) != 1:
            raise ProtocolError("authority owner has no unique qualified launcher")
        proof = matching[0].verified_gpu_launch(owner, proc_root=proc_root, profile_digest=binding.profile_digest if owner.role=="inference" else None,inspection_timeout=policy.collection_timeout_seconds)
        if proof is not None:
            proofs.append((proof,owner))
    physical = []
    for pid, kind in processes:
        start, cgroup = process_identity(pid, proc_root)
        matched = [(proof,owner) for proof,owner in proofs if cgroup == proof["cgroup"] or cgroup.startswith(proof["cgroup"] + "/")]
        if len(matched) != 1:
            raise UntrackedGPUProcess("GPU process lacks exact owned launch proof")
        proof,owner = matched[0]
        if (owner.role == "inference" and owner.reclaim_deadline is not None
                and owner.reclaim_deadline <= datetime.now(timezone.utc)):
            raise OutsideGPUFence("owned inference remained present past its existing reclaim deadline")
        physical.append({"pid": pid, "start_time": start, "class": kind,
                         "cgroup": cgroup, "launch": proof["container_id"], "role": proof["role"]})
    return {"gpu_uuid": binding.gpu_uuid, "processes": sorted(physical, key=lambda row: (row["pid"],row["class"]))}, primary


class BorrowingObserver:
    def __init__(self, config):
        from .docker_supervisor import DockerSupervisor

        self.binding = BorrowingBinding.model_validate(config["binding"])
        self.policy = BorrowingPolicy.model_validate(config["policy"])
        if policy_digest(self.policy.model_dump(mode="json")) != self.binding.policy_digest:
            raise ProtocolError("observer policy digest differs")
        self.journal = BorrowingJournal(Path(config["journal"]), self.binding)
        key_file = Path(config["reporter_key_file"])
        info = key_file.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ProtocolError("reporter signing key must be owned and private")
        self.key = key_file.read_bytes()
        if len(self.key) < 32:
            raise ProtocolError("reporter signing key is too short")
        self.latest_owners = None
        self.supervisors = {}
        for launcher in config["launchers"]:
            path = Path(launcher["journal"])
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ProtocolError("qualified launch journal must be owned and private")
            from uuid import UUID
            worker = UUID(launcher["worker_instance_id"])
            if worker in self.supervisors:
                raise ProtocolError("duplicate qualified launcher")
            self.supervisors[worker] = DockerSupervisor(path, Path(launcher["work_root"]),
                authority_id=self.binding.authority_id, host_id=self.binding.host_id,
                boot_id=self.binding.boot_id, epoch=self.binding.epoch,
                execution_scope=launcher["execution_scope"], cgroup_parent=launcher["cgroup_parent"],
                network_id=launcher["network_id"],observe_only=True)

    def accept_owners(self, body):
        signed = SignedBorrowingOwners.model_validate(body)
        verify_borrowing(signed,self.key,self.binding)
        self.latest_owners = signed
        self.journal.record_owner_view(signed,self.key)
        return signed

    def stop_borrowed(self):
        signed = self.latest_owners
        if signed is None:
            return
        view = verify_borrowing(signed,self.key,self.binding)
        for owner in view.owners:
            if (owner.role == "inference" and owner.worker_instance_id == self.binding.worker_instance_id
                    and owner.service_id == self.binding.service_id):
                supervisor = self.supervisors.get(owner.worker_instance_id)
                if supervisor is None:
                    raise ProtocolError("borrowed owner has no qualified launcher")
                supervisor.stop_verified_gpu_inference(owner,profile_digest=self.binding.profile_digest,
                    timeout=self.policy.reclaim_timeout_seconds)

    def hold(self,sequence,reason,digest,*,stop=True):
        try:
            self.journal.fault(sequence,reason,digest)
        finally:
            if stop:
                self.stop_borrowed()  # Failed persistence must still withdraw exact owned inference.

    def accept_report_ack(self, body, sequence):
        rows = body.get("borrowing_owners") if isinstance(body,dict) else None
        if not isinstance(rows,list):
            raise ProtocolError("prepared borrowing owner ACK is absent")
        matching = [signed for row in rows if (signed := SignedBorrowingOwners.model_validate(row)).observation.binding == self.binding]
        if len(matching) != 1 or body.get("sequence") != sequence or matching[0].observation.report_sequence != sequence:
            raise ProtocolError("prepared borrowing owner ACK does not match report")
        return self.accept_owners(matching[0].model_dump(mode="json"))

    def delivery_lost(self,*,stop=True):
        if self.journal.has_observation():
            signed = self.latest_owners or self.journal.owner_view()
            if signed is not None:
                self.hold(signed.observation.report_sequence,"ownership_unverified",
                    policy_digest({"condition":"authority_delivery_lost"}),stop=stop)

    async def observe_once(self, refresh):
        signed = self.latest_owners or self.journal.owner_view()
        if signed is None:
            return
        view = verify_borrowing(signed,self.key,self.binding)
        now=datetime.now(timezone.utc)
        if not fresh_observation(now,view.server_time) or not fresh_observation(now,view.report_captured_at):
            await asyncio.to_thread(self.delivery_lost)
            return
        if not view.complete:
            if self.journal.has_observation():
                await asyncio.to_thread(self.hold,view.report_sequence,"ownership_unverified",policy_digest({"condition":"owner_view_incomplete"}))
            return
        try:
            try:
                physical,primary = await asyncio.to_thread(inspect_gpu,view,self.policy,self.supervisors)
            except (UntrackedGPUProcess,OutsideGPUFence,FileNotFoundError,ProcessLookupError):
                # Re-read BOTH authority and physical samples across launch/release races.
                signed = await refresh()
                view = verify_borrowing(signed,self.key,self.binding)
                physical,primary = await asyncio.to_thread(inspect_gpu,view,self.policy,self.supervisors)
            await asyncio.to_thread(self.journal.observe,signed,self.key,physical,primary_owner=primary)
        except Exception as exc:
            reason = "outside_fence" if isinstance(exc,OutsideGPUFence) else "untracked_owner" if isinstance(exc,UntrackedGPUProcess) else "ownership_unverified"
            await asyncio.to_thread(self.hold,view.report_sequence,reason,policy_digest({"condition":reason}))

    async def run(self, client, backend_url, refresh):
        try:
            await asyncio.to_thread(self.journal.begin_run)
            while True:
                try:
                    await asyncio.wait_for(self.observe_once(refresh),timeout=self.policy.collection_timeout_seconds)
                except Exception:
                    try:
                        await asyncio.to_thread(self.delivery_lost)
                    except Exception:
                        pass  # Admission still requires a fresh successfully persisted proof.
                    logging.getLogger(__name__).error("Borrowing observation failed; inference remains held")
                try:
                    for incident in await asyncio.to_thread(self.journal.awaiting_delivery):
                        signed = self.latest_owners or self.journal.owner_view()
                        if signed is None:
                            continue
                        fault = BorrowingFault(version=1,binding=self.binding,incident=incident,
                            delivery_sequence=signed.observation.report_sequence,delivery_at=datetime.now(timezone.utc))
                        response = await client.post(backend_url.rstrip("/")+"/workers/borrowing-fault",
                            json=sign_borrowing(fault,self.key).model_dump(mode="json"))
                        response.raise_for_status()
                        ack = SignedBorrowingAck.model_validate(response.json())
                        await asyncio.to_thread(self.journal.acknowledge,ack,self.key)
                except Exception:
                    logging.getLogger(__name__).error("Borrowing incident delivery failed; inference remains held")
                await asyncio.sleep(self.policy.observation_interval_seconds)
        finally:
            try:
                await asyncio.to_thread(self.stop_borrowed)
            finally:
                await asyncio.to_thread(self.journal.finish_run)
