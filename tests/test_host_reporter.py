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


@pytest.mark.parametrize("native", [False, True])
def test_windows_execution_scopes_use_fresh_app_invoker_measurements(monkeypatch, native):
    boot = uuid4()
    config = dict(host_id=str(uuid4()), boot_id=str(boot), ram_headroom_mib=1024,
                  cpu_headroom_millicores=2000, gpu_headroom_mib={}, scratch_paths=[], scratch_headroom_mib={},
                  system_info_url="http://localhost:50070/api/system/info")
    monkeypatch.setattr(host_reporter, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(host_reporter, "physical_boot_id", lambda *args: boot)
    monkeypatch.setattr(host_reporter, "nvidia_gpu_capacity", lambda *args: {})
    monkeypatch.setattr(host_reporter, "scratch_capacities", lambda *args, **kwargs: {})
    payload = {"success": True, "data": {"capturedAt": datetime.now(timezone.utc).isoformat(),
        "host": {"ram": {"allocatable": 24000, "available": 12000}, "cpu_millicores": 20000},
        "dockerVm": {"ram": {"allocatable": 8000, "available": 4000}, "cpu_millicores": 8000}}}
    calls = []
    def collect(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)
    monkeypatch.setattr(host_reporter.httpx, "get", collect)
    if native:
        config["native_scope"] = "native"
    report = host_reporter._snapshot(config, 1)
    assert report.host_ram == Capacity(allocatable=22976, available=10976)
    assert report.cpu_millicores == 18000
    assert report.execution_scopes["/"].ram == Capacity(allocatable=6976, available=2976)
    assert report.execution_scopes["/"].cpu_millicores == 6000
    if native:
        assert report.execution_scopes["native"].ram == report.host_ram
    assert calls == [(config["system_info_url"], {"timeout": 7, "trust_env": False, "follow_redirects": False})]
    for invalid in (None, "", " ", "x" * 256, [], "/"):
        with pytest.raises(ValueError, match="native scope"):
            host_reporter._snapshot({**config, "native_scope": invalid}, 2)
    payload["success"] = False
    with pytest.raises(ValueError, match="unavailable"):
        host_reporter._snapshot(config, 2)
    payload["success"] = True
    payload["data"]["capturedAt"] = "2020-01-01T00:00:00+00:00"
    with pytest.raises(ValueError, match="stale"):
        host_reporter._snapshot(config, 2)
    with pytest.raises(ValueError, match="headroom"):
        host_reporter._snapshot({**config, "ram_headroom_mib": -1}, 2)


def test_windows_boot_identity_uses_kernel_guid_not_clock(monkeypatch):
    import ctypes
    from uuid import uuid5
    host, boot = uuid4(), uuid4()
    def query(kind, buffer, length, returned):
        assert kind == 90 and length == 32
        ctypes.memmove(buffer, boot.bytes_le + bytes(16), 32)
        returned._obj.value = 32
        return 0
    monkeypatch.setattr(host_reporter, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(host_reporter.ctypes, "WinDLL", lambda name: SimpleNamespace(NtQuerySystemInformation=query), raising=False)
    assert host_reporter.physical_boot_id(host, None) == uuid5(host, str(boot))
    assert host_reporter.physical_boot_id(host, None) == uuid5(host, str(boot))


@pytest.mark.parametrize("headers, posts", [({"Retry-After": "21600"}, 2), ({}, 15)])
def test_throttled_host_report_resumes_within_heartbeat_ceiling(monkeypatch, headers, posts):
    import asyncio
    import httpx

    clock, sent = [0.0], []
    real_client = httpx.AsyncClient

    def reply(request):
        sent.append(clock[0])
        return httpx.Response(429, headers=headers)

    async def tick(seconds):
        clock[0] += seconds
        if clock[0] >= 75:
            raise asyncio.CancelledError

    monkeypatch.setattr(host_reporter, "publish_report", lambda config: SimpleNamespace(model_dump=lambda **kw: {}))
    monkeypatch.setattr(host_reporter, "monotonic", lambda: clock[0])
    monkeypatch.setattr(host_reporter.asyncio, "sleep", tick)
    monkeypatch.setattr(host_reporter.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(reply), **kw))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(host_reporter.run({"backend_url": "http://backend/api/v1"}))
    assert len(sent) == posts
    assert max(sent) <= 70  # a 6h Retry-After parks the next post for 60s, not hours
