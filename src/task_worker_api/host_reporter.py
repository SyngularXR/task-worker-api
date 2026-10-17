"""Trusted reporter process: python -m task_worker_api.host_reporter CONFIG.

CONFIG is operator-owned JSON; keys/state must never be mounted into workers.
Only report_file is shared read-only with workers. Required fields: authority_id,
host_id, boot_id, epoch, backend_url (including /api/v1), signing_key_file,
state_file, report_file, ram_headroom_mib, cpu_headroom_millicores,
gpu_headroom_mib, scratch_headroom_mib, scratch_paths, and scope_paths (Linux)
or system_info_url and native_scope (Windows: local App Invoker system-info
endpoint and native scope identifier). Native capacity shares the host budget.
"""
import asyncio
import ctypes
from contextlib import closing, suppress
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sqlite3
import tempfile
from time import monotonic
from uuid import UUID, uuid5

import httpx

from .client import _HEARTBEAT_RETRY_AFTER_MAX_S, _retry_after_delay
from .host_resources import (
    linux_execution_scopes, linux_host_capacity, nvidia_gpu_capacity,
    scratch_capacities,
)
from .resource_protocol import SignedHostReport, observation_signature
from .resources import Capacity, ExecutionCapacity, HostSnapshot

log = logging.getLogger(__name__)


def physical_boot_id(host_id: UUID, proc_root: Path) -> UUID:
    if os.name != "nt":
        return UUID((proc_root / "sys/kernel/random/boot_id").read_text().strip())
    # SystemBootEnvironmentInformation: GUID, firmware type, padding, boot flags.
    # LastBootUpTime moves when Windows corrects its clock; it is not an identity.
    buffer = ctypes.create_string_buffer(32)
    returned = ctypes.c_ulong()
    query = ctypes.WinDLL("ntdll").NtQuerySystemInformation
    query.argtypes = [ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
    query.restype = ctypes.c_long
    status = query(90, buffer, len(buffer), ctypes.byref(returned))
    if status != 0 or returned.value != len(buffer):
        raise OSError(f"Cannot read Windows boot identity: NTSTATUS {status:#x}, size {returned.value}")
    boot = UUID(bytes_le=buffer.raw[:16])
    if not boot.int:
        raise OSError("Windows returned an empty boot identity")
    return uuid5(host_id, str(boot))


def _snapshot(config, sequence):
    host_id, boot_id = UUID(config["host_id"]), UUID(config["boot_id"])
    proc_root = Path(config.get("proc_root", "/proc"))
    if physical_boot_id(host_id, proc_root) != boot_id:
        raise ValueError("host boot changed; audited recovery is required")
    captured = datetime.now(timezone.utc)
    headroom = {"ram_headroom_mib": config["ram_headroom_mib"],
                "cpu_headroom_millicores": config["cpu_headroom_millicores"]}
    if os.name == "nt":
        if any(type(value) is not int or value < 0 for value in headroom.values()):
            raise ValueError("headroom must be a nonnegative integer")
        response = httpx.get(config["system_info_url"], timeout=7, trust_env=False, follow_redirects=False)
        response.raise_for_status()
        envelope = response.json()
        if envelope["success"] is not True:
            raise ValueError("App Invoker system information is unavailable")
        info = envelope["data"]
        observed = datetime.fromisoformat(info["capturedAt"].replace("Z", "+00:00"))
        if observed.tzinfo is None or not 0 <= (datetime.now(timezone.utc) - observed).total_seconds() <= 15:
            raise ValueError("App Invoker system information is stale")
        captured = min(captured, observed)
        measured = {}
        for name in ("host", "dockerVm"):
            capacity = ExecutionCapacity.model_validate(info[name])
            measured[name] = ExecutionCapacity(
                ram=Capacity(allocatable=max(0, capacity.ram.allocatable - headroom["ram_headroom_mib"]),
                             available=max(0, capacity.ram.available - headroom["ram_headroom_mib"])),
                cpu_millicores=max(0, capacity.cpu_millicores - headroom["cpu_headroom_millicores"]))
        ram, cpu = measured["host"].ram, measured["host"].cpu_millicores
        scopes = {"/": measured["dockerVm"]}
        if "native_scope" in config:
            native = config["native_scope"]
            if not isinstance(native, str) or not native.strip() or len(native) > 255 or native in scopes:
                raise ValueError("native scope must be a unique nonempty identifier of at most 255 characters")
            scopes[native] = {"ram": ram, "cpu_millicores": cpu}
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
    observer = None
    if "borrowing" in config:
        try:
            from .borrowing_observer import BorrowingObserver
            observer = BorrowingObserver(config["borrowing"])
            if (any(str(getattr(observer.binding,name)) != str(config[name]) for name in
                    ("authority_id","host_id","boot_id","epoch"))
                    or observer.key != Path(config["signing_key_file"]).read_bytes()):
                raise ValueError("borrowing observer differs from trusted hardware reporter")
        except Exception:
            observer = None
            log.error("Borrowing observer preparation failed; inference remains held")
    async with httpx.AsyncClient(timeout=5) as client:
        next_post = 0.0
        publication = asyncio.Lock()
        withdrawal = None

        async def borrowing_lost():
            nonlocal withdrawal
            try:
                await asyncio.to_thread(observer.delivery_lost,stop=False)
            finally:
                if withdrawal is None or withdrawal.done():
                    if withdrawal is not None:
                        with suppress(asyncio.CancelledError):
                            error = withdrawal.exception()
                            if error is not None:
                                log.error("Borrowed owned stop failed; inference remains held")
                    withdrawal = asyncio.create_task(asyncio.to_thread(observer.stop_borrowed))

        async def post_report():
            # A race refresh uses the same hardware sequence/POST ordering as the ordinary tick.
            async with publication:
                report = await asyncio.to_thread(publish_report,config)
                if monotonic() < next_post:
                    return None
                response = await client.post(config["backend_url"].rstrip("/") + "/workers/host-report",
                                             json=report.model_dump(mode="json"))
                response.raise_for_status()
                if observer is None:
                    return None
                try:
                    if len(response.content) > observer.policy.max_output_bytes:
                        raise ValueError("borrowing owner ACK exceeded qualified bound")
                    return await asyncio.to_thread(observer.accept_report_ack,response.json(),report.report.sequence)
                except Exception:
                    await borrowing_lost()
                    log.error("Borrowing owner ACK failed; inference remains held")
                    return None

        async def refresh():
            owners = await post_report()
            if owners is None:
                raise ValueError("fresh borrowing owner ACK unavailable")
            return owners

        task = asyncio.create_task(observer.run(client,config["backend_url"],refresh)) if observer is not None else None
        try:
            while True:
                try:
                    await post_report()
                except Exception as exc:
                    if observer is not None:
                        try:
                            await borrowing_lost()
                        except Exception:
                            log.error("Borrowing observation persistence failed; inference remains held")
                    if isinstance(exc,httpx.HTTPStatusError):
                        delay = _retry_after_delay(exc.response,maximum_seconds=_HEARTBEAT_RETRY_AFTER_MAX_S)
                        if delay is not None:
                            next_post = monotonic()+delay
                    log.error("Host report failed; previous observations will expire: %s",exc)
                if task is not None and task.done():
                    with suppress(asyncio.CancelledError):
                        task.exception()
                    try:
                        await borrowing_lost()
                    except Exception:
                        log.error("Borrowing observation persistence failed; inference remains held")
                    log.error("Borrowing observer stopped; inference remains held")
                    task = None
                await asyncio.sleep(5)
        finally:
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError,Exception):
                    await task
            if withdrawal is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(withdrawal),timeout=observer.policy.reclaim_timeout_seconds)
                except Exception:
                    log.error("Borrowed shutdown cleanup unproved; inference remains held")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("--once", action="store_true", help="publish one signed observation without contacting the backend")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    for name in ("signing_key_file", "state_file", "report_file"):
        config[name] = str((args.config.parent / config[name]).resolve())
    if "borrowing" in config:
        borrowing = config["borrowing"]
        for name in ("journal","reporter_key_file"):
            borrowing[name] = str((args.config.parent / borrowing[name]).resolve())
        for launcher in borrowing["launchers"]:
            for name in ("journal","work_root"):
                launcher[name] = str((args.config.parent / launcher[name]).resolve())
    logging.basicConfig(level=logging.INFO)
    if args.once:
        print(publish_report(config).model_dump_json())
    else:
        asyncio.run(run(config))
