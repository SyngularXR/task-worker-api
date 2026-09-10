"""Disposable real-GPU supervisor check; never posts evidence to a backend."""
import argparse
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from task_worker_api.docker_supervisor import DockerSupervisor
from task_worker_api.host_reporter import physical_boot_id
from task_worker_api.resources import AttemptOwnership


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--gpu", required=True)
    parser.add_argument("--network-id", required=True)
    args = parser.parse_args()
    args.root.mkdir(exist_ok=False)
    host = uuid4()
    boot = physical_boot_id(host, Path("/proc"))
    supervisor = DockerSupervisor(args.root / "private.sqlite", args.root / "work",
        authority_id=uuid4(), host_id=host, boot_id=boot, epoch=1, execution_scope="smoke-root", cgroup_parent="", network_id=args.network_id)
    claim = SimpleNamespace(host_id=host, boot_id=boot, state="reserved", gpu_uuid=args.gpu, execution_scope="smoke-root",
        ownership=AttemptOwnership(worker_instance_id=uuid4(), attempt_id=uuid4(), generation=1, token="test-only-" * 4),
        profile=SimpleNamespace(execution_ram_mib=2048, cpu_millicores=1000))
    code = """
import json, subprocess, sys
import torch
actual = subprocess.check_output(['nvidia-smi', '--query-gpu=uuid', '--format=csv,noheader'], text=True, timeout=5).strip()
assert actual == sys.argv[1], actual
tensor = torch.empty(256 * 1024, device='cuda')
torch.cuda.synchronize()
with open('/work/probe.json', 'w') as output:
    json.dump({'gpu': actual, 'allocated_bytes': torch.cuda.memory_allocated()}, output)
"""
    # Preserve the private journal on every failure for exact-ID reconciliation.
    container = supervisor.launch(claim, image=args.image, command=["python", "-c", code, args.gpu], read_only_mounts={})
    try:
        while True:
            try:
                status = supervisor._docker("wait", container)
                break
            except subprocess.TimeoutExpired:
                state = json.loads(supervisor._docker("inspect", container))[0]["State"]
                print(json.dumps({"container": container, "state": state["Status"]}), flush=True)
        if status != "0":
            raise RuntimeError(supervisor._docker("logs", container))
        observed = json.loads((supervisor.root / str(claim.ownership.attempt_id) / "probe.json").read_text())
        assert observed["allocated_bytes"] >= 1024 * 1024
    finally:
        proof = supervisor.sign_cleanup(claim, b"disposable-test-signing-key-not-production")
    assert not (supervisor.root / str(claim.ownership.attempt_id)).exists()
    assert supervisor._docker("container", "ls", "--all", "--filter", "id=" + container, "--format", "{{.ID}}") == ""
    print(json.dumps({"container": container, "observed": observed, "removed": True,
                      "cleanup": proof.observation.evidence.model_dump(mode="json")}))


if __name__ == "__main__":
    main()
