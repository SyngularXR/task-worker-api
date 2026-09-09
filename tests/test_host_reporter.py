from datetime import datetime, timezone
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

from task_worker_api import host_reporter
from task_worker_api.resource_protocol import SignedHostReport, observation_signature
from task_worker_api.resources import Capacity, HostSnapshot


@pytest.mark.parametrize("failure", ["collect", "replace"])
def test_report_sequences_survive_failure_and_binding_cannot_change(tmp_path, monkeypatch, failure):
    key = b"\nprivate-test-reporter-key-at-least-32-bytes "
    (tmp_path / "key").write_bytes(key)
    config = {"authority_id": str(uuid4()), "host_id": str(uuid4()), "boot_id": str(uuid4()), "epoch": 1,
              "signing_key_file": str(tmp_path / "key"), "state_file": str(tmp_path / "state.sqlite"),
              "report_file": str(tmp_path / "report.json")}
    def collect(config, sequence):
        return HostSnapshot(host_id=config["host_id"], boot_id=config["boot_id"], sequence=sequence,
            captured_at=datetime.now(timezone.utc), host_ram={"allocatable": 100, "available": 50},
            cpu_millicores=1000, execution_scopes={}, scratch_pools={}, gpus={})
    monkeypatch.setattr(host_reporter, "_snapshot", collect)
    first = host_reporter.publish_report(config)
    previous = (tmp_path / "report.json").read_bytes()
    def fail(*args):
        raise OSError("injected measurement/publication failure")
    with monkeypatch.context() as patch:
        if failure == "collect":
            patch.setattr(host_reporter, "_snapshot", fail)
        else:
            patch.setattr(host_reporter.os, "replace", fail)
        with pytest.raises(OSError):
            host_reporter.publish_report(config)
    assert (tmp_path / "report.json").read_bytes() == previous
    recovered = host_reporter.publish_report(dict(config))
    assert first.report.sequence == 1 and recovered.report.sequence == 3
    assert SignedHostReport.model_validate_json((tmp_path / "report.json").read_text()) == recovered
    assert recovered.signature == observation_signature("hardware", recovered.authority_id, 1,
                                                        recovered.report.model_dump(mode="json"), key)
    with pytest.raises(ValueError, match="binding changed"):
        host_reporter.publish_report({**config, "boot_id": str(uuid4())})
    assert len(list(tmp_path.iterdir())) == 3  # key, state, report; no abandoned temp file


def test_physical_boot_identity_is_stable(tmp_path):
    host = uuid4()
    if os.name != "nt":
        path = tmp_path / "sys/kernel/random"
        path.mkdir(parents=True)
        (path / "boot_id").write_text(str(uuid4()))
    assert host_reporter.physical_boot_id(host, tmp_path) == host_reporter.physical_boot_id(host, tmp_path)


@pytest.mark.parametrize("native,vm", [(True, False), (False, True), (True, True)])
def test_windows_execution_scopes_share_measured_host_budget(monkeypatch, native, vm):
    boot = uuid4()
    ram = Capacity(allocatable=24000, available=12000)
    config = dict(host_id=str(uuid4()), boot_id=str(boot), ram_headroom_mib=8192,
                  cpu_headroom_millicores=2000, gpu_headroom_mib={}, scratch_paths=[], scratch_headroom_mib={})
    monkeypatch.setattr(host_reporter, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(host_reporter, "physical_boot_id", lambda *args: boot)
    monkeypatch.setattr(host_reporter, "windows_host_capacity", lambda **kwargs: (ram, 18000))
    monkeypatch.setattr(host_reporter, "nvidia_gpu_capacity", lambda *args: {})
    monkeypatch.setattr(host_reporter, "scratch_capacities", lambda *args, **kwargs: {})
    calls = []
    def collect_vm(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(stdout='{"execution_scopes":{"vm":{"ram":{"allocatable":8000,"available":4000},"cpu_millicores":4000}}}')
    monkeypatch.setattr(host_reporter.subprocess, "run", collect_vm)
    if vm:
        config["vm_command"] = ["trusted-collector"]
    if native:
        config["native_scope"] = "native"
    report = host_reporter._snapshot(config, 1)
    assert report.host_ram == ram and report.cpu_millicores == 18000
    assert set(report.execution_scopes) == ({"native"} if native else set()) | ({"vm"} if vm else set())
    assert calls == ([["trusted-collector"]] if vm else [])
    if native:
        scope = report.execution_scopes["native"]
        assert scope.ram == ram and scope.cpu_millicores == 18000 and scope.parent is None
    if vm:
        assert report.execution_scopes["vm"].ram.available == 4000
    for invalid in (None, "", " ", "x" * 256, []):
        with pytest.raises(ValueError, match="native scope"):
            host_reporter._snapshot({**config, "native_scope": invalid}, 2)
    if vm:
        with pytest.raises(ValueError, match="native scope"):
            host_reporter._snapshot({**config, "native_scope": "vm"}, 2)
    config.pop("native_scope", None)
    config.pop("vm_command", None)
    with pytest.raises(ValueError, match="requires an execution scope"):
        host_reporter._snapshot(config, 2)
