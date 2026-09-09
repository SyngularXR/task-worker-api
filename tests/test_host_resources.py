import pytest
import os

from task_worker_api.host_resources import MIB, linux_execution_scopes, linux_host_capacity, windows_host_capacity
from task_worker_api.resources import Capacity


def test_scratch_aliases_share_one_budget_and_inventory_is_fixed(monkeypatch, tmp_path):
    import types
    from task_worker_api.host_resources import scratch_capacities

    monkeypatch.setattr("task_worker_api.host_resources.filesystem_identity", lambda path: "volume:test")
    readings = iter([types.SimpleNamespace(total=1000 * MIB, free=700 * MIB),
                     types.SimpleNamespace(total=1000 * MIB, free=600 * MIB)])
    monkeypatch.setattr("task_worker_api.host_resources.shutil.disk_usage", lambda path: next(readings))
    assert scratch_capacities([tmp_path, tmp_path / "alias"], headroom_mib={"volume:test": 100}) == {
        "volume:test": Capacity(allocatable=900, available=500)}
    with pytest.raises(ValueError, match="differs from enrolled"):
        scratch_capacities([tmp_path], headroom_mib={"volume:other": 100})
    with pytest.raises(ValueError, match="volume is missing"):
        scratch_capacities([], headroom_mib={"volume:test": 100})


@pytest.mark.skipif(os.name != "nt", reason="native Windows volume identity")
def test_windows_volume_alias_identity(tmp_path):
    from task_worker_api.host_resources import filesystem_identity, scratch_capacities

    child = tmp_path / "child"
    child.mkdir()
    identity = filesystem_identity(tmp_path)
    assert filesystem_identity(child) == identity
    assert identity.startswith("volume:")
    assert len(scratch_capacities([tmp_path, child], headroom_mib={identity: 0})) == 1


@pytest.mark.skipif(os.name == "nt", reason="Linux findmnt adapter")
@pytest.mark.parametrize("value", ["", "39b79433-6156-49a2-83e2-b8a3fcbf417c"])
def test_linux_filesystem_identity_requires_uuid(monkeypatch, tmp_path, value):
    import types
    from task_worker_api.host_resources import filesystem_identity

    def measure(command, **kwargs):
        assert command[0] == "findmnt" and command[-1] == str(tmp_path)
        assert kwargs["timeout"] == 5 and kwargs["check"]
        return types.SimpleNamespace(stdout=value)
    monkeypatch.setattr("task_worker_api.host_resources.subprocess.run", measure)
    if value:
        assert filesystem_identity(tmp_path) == "filesystem:" + value
    else:
        with pytest.raises(ValueError, match="stable filesystem UUID unavailable"):
            filesystem_identity(tmp_path)


@pytest.mark.parametrize("row", ["", "GPU-wrong, 100, 50", "GPU-00000000-0000-0000-0000-000000000001, N/A, 50",
    "GPU-00000000-0000-0000-0000-000000000001, 100, 101"])
def test_gpu_collector_rejects_missing_or_invalid_measurements(monkeypatch, row):
    import types
    from task_worker_api.host_resources import nvidia_gpu_capacity

    monkeypatch.setattr("task_worker_api.host_resources.subprocess.run", lambda *args, **kwargs: types.SimpleNamespace(stdout=row))
    with pytest.raises(ValueError):
        nvidia_gpu_capacity({"GPU-00000000-0000-0000-0000-000000000001": 10})


def test_gpu_collector_uses_uuid_headroom_and_bounded_command(monkeypatch):
    import types
    from task_worker_api.host_resources import nvidia_gpu_capacity

    device = "GPU-00000000-0000-0000-0000-000000000001"
    def measure(command, **kwargs):
        assert "uuid,memory.total,memory.free" in command[1]
        assert kwargs["timeout"] == 5 and kwargs["check"]
        return types.SimpleNamespace(stdout=f"{device}, 24000, 21000\n")
    monkeypatch.setattr("task_worker_api.host_resources.subprocess.run", measure)
    assert nvidia_gpu_capacity({device: 1000}) == {device: Capacity(allocatable=23000, available=20000)}


@pytest.mark.skipif(os.name != "nt", reason="native Windows memory API")
def test_windows_physical_host_capacity():
    ram, cpu = windows_host_capacity(ram_headroom_mib=0, cpu_headroom_millicores=0)
    reserved_ram, reserved_cpu = windows_host_capacity(ram_headroom_mib=1, cpu_headroom_millicores=100)
    assert ram.allocatable > 1 and 0 <= ram.available <= ram.allocatable
    assert reserved_ram.allocatable == ram.allocatable - 1
    assert reserved_cpu == max(0, cpu - 100)


def test_host_and_shared_cgroup_measurements(tmp_path):
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "meminfo").write_text("MemTotal: 8192000 kB\nMemAvailable: 4096000 kB\n")
    (proc / "stat").write_text("cpu 0\ncpu0 0\ncpu1 0\ncpu2 0\ncpu3 0\n")
    ram, cpu = linux_host_capacity(ram_headroom_mib=100, cpu_headroom_millicores=100, proc_root=proc)
    assert ram == Capacity(allocatable=7900, available=3900) and cpu == 3900
    root = tmp_path / "cgroup"
    for name, limit, used, quota in [("shared", 2000 * MIB, 600 * MIB, "150000 100000"),
                                    ("shared/a", "max", 100 * MIB, "max 100000"),
                                    ("shared/b", 3000 * MIB, 100 * MIB, "200000 100000")]:
        path = root / name
        path.mkdir(parents=True)
        for file, value in {"memory.max": limit, "memory.current": used, "cpu.max": quota,
                            "cpuset.cpus.effective": "0-1,3"}.items():
            (path / file).write_text(str(value))
    scopes = linux_execution_scopes(["/shared/a", "/shared/b", "/shared/a"], host_ram=ram,
        host_cpu_millicores=cpu, ram_headroom_mib=100, cpu_headroom_millicores=100, cgroup_root=root)
    assert len(scopes) == 4 and scopes["/"].ram == ram
    assert scopes["/shared/a"].parent == scopes["/shared/b"].parent == "/shared"
    assert scopes["/shared"].ram == Capacity(allocatable=1900, available=1300)
    assert scopes["/shared"].cpu_millicores == 1400
    assert scopes["/shared/a"].cpu_millicores == 2900
    assert scopes["/shared/b"].cpu_millicores == 1900
    assert scopes["/shared/b"].ram.available == 2800
    (root / "shared/a/cpu.max").unlink()
    with pytest.raises(FileNotFoundError):
        linux_execution_scopes(["/shared/a"], host_ram=ram, host_cpu_millicores=cpu,
            ram_headroom_mib=100, cpu_headroom_millicores=100, cgroup_root=root)


@pytest.mark.parametrize("path", ["relative", "/../outside", "/a/../../outside", "C:\\outside"])
def test_cgroup_paths_cannot_escape_root(tmp_path, path):
    with pytest.raises(ValueError):
        linux_execution_scopes([path], host_ram=Capacity(allocatable=1, available=1),
            host_cpu_millicores=1000, ram_headroom_mib=0, cpu_headroom_millicores=0, cgroup_root=tmp_path)
