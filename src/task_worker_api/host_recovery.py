"""Read-only reboot inspection: python -m task_worker_api.host_recovery CONFIG.

CONFIG contains reporter (the old enrolled reporter config), operation_id, reason,
expected_attempts (backend old-boot attempt UUIDs), and the complete enrolled
journal inventory (including journals with no launches). Each journal has
kind (docker/windows), path and work_root. Run while admission and supervisors
are drained. This command never stops containers, deletes files or posts recovery.
It refuses leftover scratch; Windows also requires recorded profile/ACL cleanup.
The signed JSON is submitted by a super-admin to /hosts/{id}/recover immediately.
--cleanup-windows first cleans recorded native resources after verifying reboot.
"""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from uuid import UUID

from .host_reporter import _snapshot, physical_boot_id
from .host_resources import filesystem_identity
from .resource_protocol import observation_signature
from .resources import AdmissionError


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _docker_inventory():
    result = subprocess.run(["docker", "container", "ls", "--all", "--no-trunc", "--format", "{{json .}}"],
        check=True, capture_output=True, text=True, timeout=30,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def cleanup_windows_after_reboot(config):
    """Explicit cleanup mode; the default collector remains read-only."""
    from .windows_launch import WindowsLaunchJournal

    reporter = config["reporter"]
    host, old_boot = UUID(reporter["host_id"]), UUID(reporter["boot_id"])
    new_boot = physical_boot_id(host, Path(reporter.get("proc_root", "/proc")))
    if new_boot == old_boot:
        raise AdmissionError("host_has_not_rebooted")
    expected = {str(UUID(value)) for value in config["expected_attempts"]}
    pending = []
    for entry in config["journals"]:
        if entry["kind"] != "windows":
            continue
        path = Path(entry["path"]).resolve(strict=True)
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            rows = db.execute("SELECT attempt,binding FROM windows_launches").fetchall()
        for attempt, raw in rows:
            binding = json.loads(raw)
            if binding["boot"] != str(old_boot):
                continue
            if (attempt not in expected or binding["host"] != str(host)
                    or binding["authority"] != reporter["authority_id"]
                    or not 0 < binding["epoch"] <= reporter["epoch"]):
                raise AdmissionError("attempt_fenced")
            pending.append((entry, binding, attempt))
    for entry, binding, attempt in pending:
        journal = WindowsLaunchJournal(entry["path"], work_root=entry["work_root"],
            authority_id=UUID(reporter["authority_id"]), epoch=binding["epoch"], host_id=host,
            boot_id=old_boot, execution_scope=binding["scope"])
        journal.cleanup_after_reboot(UUID(attempt), new_boot)


def inspect_reboot(config):
    reporter = config["reporter"]
    host, old_boot = UUID(reporter["host_id"]), UUID(reporter["boot_id"])
    boot = physical_boot_id(host, Path(reporter.get("proc_root", "/proc")))
    if boot == old_boot:
        raise AdmissionError("host_has_not_rebooted")
    started = datetime.now(timezone.utc)
    launches, storage, journal_receipts = {}, {}, []
    if not config["journals"]:
        raise AdmissionError("launch_inventory_incomplete")
    containers = _docker_inventory() if any(j["kind"] == "docker" for j in config["journals"]) else []
    for entry in config["journals"]:
        root = Path(entry["work_root"])
        journal = Path(entry["path"])
        if (not root.is_absolute() or root.resolve(strict=True) != root or not root.is_dir()
                or journal.resolve(strict=True).is_relative_to(root)):
            raise AdmissionError("unsafe_cleanup_path")
        if any(root.iterdir()):
            raise AdmissionError("scratch_cleanup_required")
        pool = filesystem_identity(root)
        identity = root.stat()
        storage.setdefault(pool, []).append([str(root), identity.st_dev, identity.st_ino])
        with closing(sqlite3.connect(journal.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            if entry["kind"] == "docker":
                rows = db.execute("SELECT attempt,plan,phase,container FROM launches").fetchall()
            elif entry["kind"] == "windows":
                rows = db.execute("SELECT attempt,binding,phase,plan,pid,creation_time FROM windows_launches").fetchall()
            else:
                raise AdmissionError("unknown_supervisor_kind")
        journal_receipts.append([str(journal.resolve()), _digest(sorted(rows))])
        for row in rows:
            attempt, raw, phase = row[:3]
            plan = json.loads(raw)
            if plan["boot"] != str(old_boot):
                continue
            if (plan["host"] != str(host) or plan["authority"] != reporter["authority_id"]
                    or plan["root"] != str(root) or not 0 < plan["epoch"] <= reporter["epoch"]
                    or plan["ownership"]["attempt_id"] != attempt or attempt in launches):
                raise AdmissionError("attempt_fenced")
            if entry["kind"] == "docker":
                container = row[3]
                if any(item["ID"] == container or item["Names"] == plan["name"]
                       or "synpusher.launch=" + plan["id"] in item.get("Labels", "").split(",")
                       for item in containers):
                    raise AdmissionError("launch_cleanup_required")
            elif phase != "access_cleaned":
                # Reboot proves exit, but does not revoke persistent AppContainer grants.
                raise AdmissionError("windows_access_cleanup_required")
            launches[attempt] = _digest(row)
    expected = {str(UUID(value)) for value in config["expected_attempts"]}
    if not set(launches) <= expected:
        raise AdmissionError("launch_inventory_incomplete")
    for attempt in expected - launches.keys():
        # Both supervisors durably insert the intent before creating any launch
        # or native profile. Absence is meaningful only across all enrolled journals.
        name = "synpusher-attempt-" + UUID(attempt).hex
        if any(item["Names"] == name for item in containers):
            raise AdmissionError("launch_cleanup_required")
        launches[attempt] = _digest({"not_launched": attempt, "journals": journal_receipts,
            "boot": str(boot), "storage": storage})
    snapshot = _snapshot({**reporter, "boot_id": str(boot)}, 1)
    if storage.keys() != snapshot.scratch_pools.keys():
        raise AdmissionError("storage_inventory_incomplete")
    if physical_boot_id(host, Path(reporter.get("proc_root", "/proc"))) != boot:
        raise AdmissionError("attempt_fenced")
    if (datetime.now(timezone.utc) - started).total_seconds() > 15:
        raise AdmissionError("recovery_inspection_stale")
    inspection = {"previous_boot_id": str(old_boot), "snapshot": snapshot.model_dump(mode="json"),
        "stopped_launches": launches, "cleaned_storage": {pool: _digest(roots) for pool, roots in storage.items()}}
    return {"operation_id": str(UUID(config["operation_id"])), "action": "verify_reboot", "reason": config["reason"],
        "authority_id": reporter["authority_id"], "epoch": reporter["epoch"], "inspection": inspection,
        "signature": observation_signature("reboot", UUID(reporter["authority_id"]), reporter["epoch"], inspection,
            Path(reporter["signing_key_file"]).read_bytes())}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("--cleanup-windows", action="store_true")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    if args.cleanup_windows:
        cleanup_windows_after_reboot(config)
    print(json.dumps(inspect_reboot(config)))
