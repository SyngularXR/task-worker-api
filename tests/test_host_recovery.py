from datetime import datetime, timezone
import json
import sqlite3
from uuid import uuid4

import pytest

from task_worker_api import host_recovery as recovery
from task_worker_api.resources import AdmissionError, HostSnapshot
from task_worker_api.resource_protocol import observation_signature


@pytest.mark.parametrize("fault", [None, "same_boot", "container", "scratch", "missing_attempt", "foreign_host", "windows_grants",
    "unlaunched", "orphan_without_journal", "missing_journal", "retained_pool", "missing_pool"])
def test_reboot_inspection_reads_journal_and_refuses_leftovers(tmp_path, monkeypatch, fault):
    host, old_boot, new_boot, authority, attempt = [uuid4() for _ in range(5)]
    root = tmp_path / "scratch"
    root.mkdir()
    journal = tmp_path / "journal.sqlite"
    key = b"test-reboot-signing-key-123456789012345"
    (tmp_path / "key").write_bytes(key)
    plan = {"host": str(uuid4() if fault == "foreign_host" else host), "boot": str(old_boot),
        "authority": str(authority), "epoch": 1, "root": str(root), "ownership": {"attempt_id": str(attempt)},
        "id": "immutable-launch", "name": "attempt-container"}
    with sqlite3.connect(journal) as db:
        if fault == "windows_grants":
            db.execute("CREATE TABLE windows_launches(attempt,binding,phase,plan,pid,creation_time)")
            db.execute("INSERT INTO windows_launches VALUES(?,?,'started','{}',123,456)", [str(attempt), json.dumps(plan)])
        else:
            db.execute("CREATE TABLE launches(attempt,plan,phase,container)")
            db.execute("INSERT INTO launches VALUES(?,?,'started','immutable-container')", [str(attempt), json.dumps(plan)])
        if fault in ("unlaunched", "orphan_without_journal"):
            db.execute("DELETE FROM launches")
    if fault == "scratch":
        (root / "leftover").write_text("never delete me")
    monkeypatch.setattr(recovery, "physical_boot_id", lambda *a: old_boot if fault == "same_boot" else new_boot)
    retained = tmp_path / "retained"
    retained.mkdir()
    (retained / "artifact").write_text("preserve me")
    monkeypatch.setattr(recovery, "filesystem_identity", lambda p: "volume-retained" if p == retained else "volume-test")
    monkeypatch.setattr(recovery, "_docker_inventory", lambda:
        [{"ID": "immutable-container", "Names": "attempt-container"}] if fault == "container" else
        [{"ID": "orphan", "Names": "synpusher-attempt-" + attempt.hex}] if fault == "orphan_without_journal" else [])
    monkeypatch.setattr(recovery, "_snapshot", lambda config, seq: HostSnapshot(
        host_id=host, boot_id=new_boot, sequence=seq, captured_at=datetime.now(timezone.utc),
        host_ram={"allocatable": 100, "available": 100}, cpu_millicores=1000, execution_scopes={}, gpus={},
        scratch_pools={pool: {"allocatable": 100, "available": 100} for pool in
            (["volume-test", "volume-retained"] if fault in ("retained_pool", "missing_pool") else ["volume-test"])}))
    config = {"reporter": {"host_id": str(host), "boot_id": str(old_boot), "authority_id": str(authority),
        "epoch": 1, "signing_key_file": str(tmp_path / "key")}, "operation_id": str(uuid4()), "reason": "test reboot",
        "expected_attempts": [] if fault == "missing_attempt" else [str(attempt)],
        "journals": [{"kind": "windows" if fault == "windows_grants" else "docker", "path": str(journal), "work_root": str(root)}]}
    before = journal.read_bytes()
    if fault == "retained_pool":
        config["reporter"]["scratch_paths"] = [str(retained)]
    if fault == "missing_journal":
        config["journals"] = []
    if fault and fault not in ("unlaunched", "retained_pool"):
        with pytest.raises(AdmissionError):
            recovery.inspect_reboot(config)
    else:
        result = recovery.inspect_reboot(config)
        assert result["signature"] == observation_signature("reboot", authority, 1, result["inspection"], key)
        assert set(result["inspection"]["stopped_launches"]) == {str(attempt)}
        if fault == "retained_pool":
            assert set(result["inspection"]["cleaned_storage"]) == {"volume-test", "volume-retained"}
    assert (retained / "artifact").read_text() == "preserve me"
    assert journal.read_bytes() == before
    if fault == "scratch":
        assert (root / "leftover").read_text() == "never delete me"
