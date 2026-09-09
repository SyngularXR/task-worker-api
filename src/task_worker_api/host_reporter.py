"""Trusted reporter process: python -m task_worker_api.host_reporter CONFIG.

CONFIG is operator-owned JSON; keys/state must never be mounted into workers.
Only report_file is shared read-only with workers. Required fields: authority_id,
host_id, boot_id, epoch, backend_url (including /api/v1), signing_key_file,
state_file, report_file, ram_headroom_mib, cpu_headroom_millicores,
gpu_headroom_mib, scratch_headroom_mib, scratch_paths, and scope_paths (Linux)
or native_scope and/or vm_command (Windows: native scope identifier and argv
for the trusted Linux scope collector). Native capacity shares the host budget.
"""
import asyncio
from contextlib import closing
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
from time import monotonic
from uuid import UUID, uuid5

import httpx

from .client import _retry_after_delay
from .host_resources import (
    linux_execution_scopes, linux_host_capacity, nvidia_gpu_capacity,
    scratch_capacities, windows_host_capacity,
)
from .resource_protocol import SignedHostReport, observation_signature
from .resources import HostSnapshot

log = logging.getLogger(__name__)


def physical_boot_id(host_id: UUID, proc_root: Path) -> UUID:
    if os.name != "nt":
        return UUID((proc_root / "sys/kernel/random/boot_id").read_text().strip())
    boot = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
        "(Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().ToString('o')"],
        check=True, capture_output=True, text=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW).stdout.strip()
    datetime.fromisoformat(boot.replace("Z", "+00:00"))  # Reject empty/error output.
    return uuid5(host_id, boot)


def _snapshot(config, sequence):
    host_id, boot_id = UUID(config["host_id"]), UUID(config["boot_id"])
    proc_root = Path(config.get("proc_root", "/proc"))
    if physical_boot_id(host_id, proc_root) != boot_id:
        raise ValueError("host boot changed; audited recovery is required")
    captured = datetime.now(timezone.utc)
    headroom = {"ram_headroom_mib": config["ram_headroom_mib"],
                "cpu_headroom_millicores": config["cpu_headroom_millicores"]}
    if os.name == "nt":
        ram, cpu = windows_host_capacity(**headroom)
        scopes = {}
        if "vm_command" in config:
            # Operator-provisioned argv, never shell text or a worker request.
            raw = subprocess.run(config["vm_command"], check=True, capture_output=True, text=True, timeout=10).stdout
            scopes = json.loads(raw)["execution_scopes"]
        if "native_scope" in config:
            native = config["native_scope"]
            if not isinstance(native, str) or not native.strip() or len(native) > 255 or native in scopes:
                raise ValueError("native scope must be a unique nonempty identifier of at most 255 characters")
            scopes[native] = {"ram": ram, "cpu_millicores": cpu}
        if not scopes:
            raise ValueError("Windows reporter requires an execution scope")
    else:
        ram, cpu = linux_host_capacity(**headroom, proc_root=proc_root)
        scopes = linux_execution_scopes(config["scope_paths"], host_ram=ram, host_cpu_millicores=cpu,
            **headroom, cgroup_root=Path(config.get("cgroup_root", "/sys/fs/cgroup")))
    snapshot = HostSnapshot(host_id=host_id, boot_id=boot_id, sequence=sequence, captured_at=captured,
        host_ram=ram, cpu_millicores=cpu, execution_scopes=scopes,
        gpus=nvidia_gpu_capacity(config["gpu_headroom_mib"]),
        scratch_pools=scratch_capacities([Path(path) for path in config["scratch_paths"]],
                                        headroom_mib=config["scratch_headroom_mib"]))
    if not 0 <= (datetime.now(timezone.utc) - captured).total_seconds() <= 15:
        raise ValueError("hardware collection exceeded the observation freshness window")
    return snapshot


def publish_report(config) -> SignedHostReport:
    """Commit sequence before collecting; a crash may skip numbers, never reuse one."""
    authority = UUID(config["authority_id"])
    binding = json.dumps({key: config[key] for key in ("authority_id", "host_id", "boot_id", "epoch")}, sort_keys=True)
    key = Path(config["signing_key_file"]).read_bytes()
    if len(key) < 32:
        raise ValueError("reporter signing key requires at least 32 bytes")
    with closing(sqlite3.connect(config["state_file"])) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS reporter (id INTEGER PRIMARY KEY CHECK(id=1), binding TEXT NOT NULL, sequence INTEGER NOT NULL)")
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT binding,sequence FROM reporter WHERE id=1").fetchone()
        if row and row[0] != binding:
            raise ValueError("reporter binding changed; audited recovery is required")
        sequence = row[1] + 1 if row else 1
        db.execute("INSERT INTO reporter VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET sequence=excluded.sequence", [binding, sequence])
    snapshot = _snapshot(config, sequence)
    report = SignedHostReport(authority_id=authority, epoch=config["epoch"], report=snapshot,
        signature=observation_signature("hardware", authority, config["epoch"], snapshot.model_dump(mode="json"), key))
    destination = Path(config["report_file"])
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(report.model_dump_json())
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)  # Signed telemetry is readable; the signing key is separate.
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return report


async def run(config):
    async with httpx.AsyncClient(timeout=5) as client:
        next_post = 0.0
        while True:
            try:
                report = await asyncio.to_thread(publish_report, config)
                if monotonic() >= next_post:
                    response = await client.post(config["backend_url"].rstrip("/") + "/workers/host-report",
                                                 json=report.model_dump(mode="json"))
                    response.raise_for_status()
            except Exception as exc:
                if isinstance(exc, httpx.HTTPStatusError):
                    delay = _retry_after_delay(exc.response, maximum_seconds=None)
                    if delay is not None:
                        next_post = monotonic() + delay
                log.error("Host report failed; previous observations will expire: %s", exc)
            await asyncio.sleep(5)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("--once", action="store_true", help="publish one signed observation without contacting the backend")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    for name in ("signing_key_file", "state_file", "report_file"):
        config[name] = str((args.config.parent / config[name]).resolve())
    logging.basicConfig(level=logging.INFO)
    if args.once:
        print(publish_report(config).model_dump_json())
    else:
        asyncio.run(run(config))
