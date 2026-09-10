"""Physical host and Linux cgroup-v2 measurements for the trusted reporter.

The CLI requires host /proc and /sys/fs/cgroup visibility. Container-local namespaces
cannot establish host limits. This module collects measurements, not cleanup
proofs, worker permissions, or validated workload budgets.
"""
from pathlib import Path, PurePosixPath
import os
import csv
import subprocess
import shutil
from uuid import UUID

from .resources import Capacity, ExecutionCapacity

MIB = 1024 * 1024


def _nonnegative(value: int):
    if type(value) is not int or value < 0:
        raise ValueError("resource headroom must be a nonnegative integer")


def filesystem_identity(path: Path) -> str:
    """Resolve a local backing volume, never a worker-chosen mount alias."""
    path = path.resolve(strict=True)
    if not path.is_dir():
        raise ValueError("scratch path must be a directory")
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        mount = ctypes.create_unicode_buffer(32768)
        volume = ctypes.create_unicode_buffer(64)
        for name, source, target in (("GetVolumePathNameW", str(path), mount),
                                     ("GetVolumeNameForVolumeMountPointW", mount, volume)):
            query = getattr(kernel, name)
            query.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
            query.restype = wintypes.BOOL
            if not query(source, target, len(target)):
                raise ctypes.WinError(ctypes.get_last_error())
        return "volume:" + str(UUID(volume.value.split("{", 1)[1].split("}", 1)[0]))
    value = subprocess.run(["findmnt", "--noheadings", "--raw", "--output", "UUID", "--target", str(path)],
                           check=True, capture_output=True, text=True, timeout=5).stdout.strip()
    # Unknown/overlay filesystems require host mount visibility, not a path-based
    # substitute that would count aliases as independent disks.
    try:
        return "filesystem:" + str(UUID(value))
    except ValueError as exc:
        raise ValueError("stable filesystem UUID unavailable; verify host mount and device-read access") from exc


def scratch_capacities(paths: list[Path], *, headroom_mib: dict[str, int]) -> dict[str, Capacity]:
    """Read enrolled volumes once logically, coalescing aliases conservatively."""
    for reserve in headroom_mib.values():
        _nonnegative(reserve)
    result = {}
    for path in paths:
        identity = filesystem_identity(path)
        if identity not in headroom_mib:
            raise ValueError("scratch volume differs from enrolled inventory")
        usage = shutil.disk_usage(path)
        if not 0 <= usage.free <= usage.total:
            raise ValueError("invalid scratch capacity measurement")
        reserve = headroom_mib[identity]
        capacity = Capacity(allocatable=max(0, usage.total // MIB - reserve),
                            available=max(0, usage.free // MIB - reserve))
        if identity in result:
            previous = result[identity]
            capacity = Capacity(allocatable=min(previous.allocatable, capacity.allocatable),
                                available=min(previous.available, capacity.available))
        result[identity] = capacity
    if result.keys() != headroom_mib.keys():
        raise ValueError("enrolled scratch volume is missing")
    return result


def nvidia_gpu_capacity(headroom_mib: dict[str, int], *, executable: str = "nvidia-smi") -> dict[str, Capacity]:
    """Measure enrolled physical GPUs; unknown/missing inventory fails closed.

    A successful reading is capacity information only, never proof that a prior
    attempt's processes or model references have been cleaned up.
    """
    for device, reserve in headroom_mib.items():
        if not device.startswith("GPU-") or str(UUID(device[4:])) != device[4:]:
            raise ValueError("GPU headroom must be keyed by physical UUID")
        _nonnegative(reserve)
    if not headroom_mib:
        return {}
    output = subprocess.run([executable, "--query-gpu=uuid,memory.total,memory.free",
                             "--format=csv,noheader,nounits"],
                            check=True, capture_output=True, text=True, timeout=5).stdout
    measured = {}
    for row in csv.reader(output.splitlines(), skipinitialspace=True):
        if len(row) != 3:
            raise ValueError("invalid NVIDIA capacity row")
        device, total, free = [value.strip() for value in row]
        if device not in headroom_mib or device in measured:
            raise ValueError("NVIDIA inventory differs from enrolled physical GPUs")
        total, free = int(total), int(free)
        if total <= 0 or not 0 <= free <= total:
            raise ValueError("invalid NVIDIA memory measurement")
        reserve = headroom_mib[device]
        measured[device] = Capacity(allocatable=max(0, total - reserve), available=max(0, free - reserve))
    if measured.keys() != headroom_mib.keys():
        raise ValueError("enrolled GPU is missing from NVIDIA inventory")
    return measured


def windows_host_capacity(*, ram_headroom_mib: int, cpu_headroom_millicores: int) -> tuple[Capacity, int]:
    """Physical Windows host capacity, separate from Docker Desktop's Linux VM."""
    import ctypes
    from ctypes import wintypes

    _nonnegative(ram_headroom_mib)
    _nonnegative(cpu_headroom_millicores)

    class MemoryStatus(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD),
                    ("total_phys", ctypes.c_ulonglong), ("avail_phys", ctypes.c_ulonglong),
                    ("total_pagefile", ctypes.c_ulonglong), ("avail_pagefile", ctypes.c_ulonglong),
                    ("total_virtual", ctypes.c_ulonglong), ("avail_virtual", ctypes.c_ulonglong),
                    ("avail_extended_virtual", ctypes.c_ulonglong)]

    status = MemoryStatus()
    status.length = ctypes.sizeof(status)
    query = ctypes.WinDLL("kernel32", use_last_error=True).GlobalMemoryStatusEx
    query.argtypes = [ctypes.POINTER(MemoryStatus)]
    query.restype = wintypes.BOOL
    if not query(ctypes.byref(status)):
        raise ctypes.WinError(ctypes.get_last_error())
    cpus = os.cpu_count()
    if cpus is None:
        raise ValueError("host CPU inventory is unavailable")
    return (Capacity(allocatable=max(0, status.total_phys // MIB - ram_headroom_mib),
                     available=max(0, status.avail_phys // MIB - ram_headroom_mib)),
            max(0, cpus * 1000 - cpu_headroom_millicores))


def linux_host_capacity(*, ram_headroom_mib: int, cpu_headroom_millicores: int,
                        proc_root: Path = Path("/proc")) -> tuple[Capacity, int]:
    _nonnegative(ram_headroom_mib)
    _nonnegative(cpu_headroom_millicores)
    memory = {}
    for line in (proc_root / "meminfo").read_text().splitlines():
        fields = line.split()
        if fields[0] in ("MemTotal:", "MemAvailable:"):
            if len(fields) != 3 or fields[2] != "kB" or int(fields[1]) < 0:
                raise ValueError("invalid host memory measurement")
            memory[fields[0]] = int(fields[1]) // 1024
    # Missing MemAvailable is not interpreted as free memory.
    ram = Capacity(allocatable=max(0, memory["MemTotal:"] - ram_headroom_mib),
                   available=max(0, memory["MemAvailable:"] - ram_headroom_mib))
    cpus = sum(line.split()[0][3:].isdigit() for line in (proc_root / "stat").read_text().splitlines()
               if line.startswith("cpu"))
    if cpus == 0:
        raise ValueError("host CPU inventory is empty")
    return ram, max(0, cpus * 1000 - cpu_headroom_millicores)


def _cpuset_count(text: str) -> int:
    count, previous = 0, -1
    for entry in text.strip().split(",") if text.strip() else []:
        bounds = entry.split("-")
        if len(bounds) not in (1, 2):
            raise ValueError("invalid effective CPU set")
        first, last = int(bounds[0]), int(bounds[-1])
        if first <= previous or last < first:
            raise ValueError("invalid effective CPU set")
        count += last - first + 1
        previous = last
    return count


def linux_execution_scopes(paths: list[str], *, host_ram: Capacity, host_cpu_millicores: int,
                           ram_headroom_mib: int, cpu_headroom_millicores: int,
                           cgroup_root: Path = Path("/sys/fs/cgroup")) -> dict[str, ExecutionCapacity]:
    """Read each leaf and every ancestor; retain shared parents as shared scopes.

    Headroom is explicit. The root uses already-adjusted host capacity; a finite
    child limit gets its own headroom without subtracting host headroom twice.
    Missing controller measurements fail rather than inventing unlimited limits.
    """
    for quantity in (host_cpu_millicores, ram_headroom_mib, cpu_headroom_millicores):
        _nonnegative(quantity)
    root = cgroup_root.resolve(strict=True)
    directories = {root}
    for path in paths:
        relative = PurePosixPath(path)
        if not path.startswith("/") or ".." in relative.parts or "\\" in path:
            raise ValueError("cgroup path must be host-absolute and remain under the cgroup root")
        current = root.joinpath(*relative.parts[1:]).resolve(strict=True)
        current.relative_to(root)  # Reject a symlink escaping the host cgroup mount.
        while current != root:
            directories.add(current)
            current = current.parent
    result = {"/": ExecutionCapacity(ram=host_ram, cpu_millicores=host_cpu_millicores)}
    for directory in sorted(directories - {root}):
        scope = "/" + directory.relative_to(root).as_posix()
        parent = "/" + directory.parent.relative_to(root).as_posix()
        parent = "/" if parent == "/." else parent
        raw_limit = (directory / "memory.max").read_text().strip()
        used = int((directory / "memory.current").read_text())
        _nonnegative(used)
        ram = host_ram
        if raw_limit != "max":
            limit = int(raw_limit)
            _nonnegative(limit)
            total = min(host_ram.allocatable, max(0, limit // MIB - ram_headroom_mib))
            free = min(host_ram.available, total, max(0, (limit - used) // MIB - ram_headroom_mib))
            ram = Capacity(allocatable=total, available=free)
        quota, period = (directory / "cpu.max").read_text().split()
        if int(period) <= 0:
            raise ValueError("invalid CPU quota period")
        cpus = _cpuset_count((directory / "cpuset.cpus.effective").read_text()) * 1000
        cpu = min(host_cpu_millicores, max(0, cpus - cpu_headroom_millicores))
        if quota != "max":
            if int(quota) <= 0:
                raise ValueError("invalid CPU quota")
            cpu = min(cpu, max(0, int(quota) * 1000 // int(period) - cpu_headroom_millicores))
        result[scope] = ExecutionCapacity(ram=ram, cpu_millicores=cpu, parent=parent)
    return result


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", action="append", required=True)
    parser.add_argument("--ram-headroom-mib", type=int, required=True)
    parser.add_argument("--cpu-headroom-millicores", type=int, required=True)
    args = parser.parse_args()
    host_ram, host_cpu = linux_host_capacity(ram_headroom_mib=args.ram_headroom_mib,
                                            cpu_headroom_millicores=args.cpu_headroom_millicores)
    scopes = linux_execution_scopes(args.scope, host_ram=host_ram, host_cpu_millicores=host_cpu,
        ram_headroom_mib=args.ram_headroom_mib, cpu_headroom_millicores=args.cpu_headroom_millicores)
    print(json.dumps({"host_ram": host_ram.model_dump(), "cpu_millicores": host_cpu,
                      "execution_scopes": {key: value.model_dump() for key, value in scopes.items()}}))
