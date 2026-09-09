import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

import pytest

from task_worker_api.windows_job import WindowsJob
from task_worker_api.windows_job import reconcile_windows_process
from task_worker_api.host_reporter import physical_boot_id
from task_worker_api.resources import AdmissionError

pytestmark = pytest.mark.skipif(os.name != "nt", reason="real Windows job-object checks")


def environment():
    return {"SystemRoot": os.environ["SystemRoot"]}


def await_file(path):
    deadline = time.monotonic() + 10
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert path.exists()


def child_handle(pid):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x100000 | 0x1000, False, pid)
    assert handle, ctypes.get_last_error()
    return kernel, handle


def test_atomic_assignment_suspension_environment_and_limits(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPERVISOR_PRIVATE_TEST", "must not inherit")
    output = tmp_path / "result.json"
    script = "import json,os,pathlib,sys; print('owned log'); pathlib.Path(sys.argv[1]).write_text(json.dumps([sys.argv[2:],os.getenv('SUPERVISOR_PRIVATE_TEST')]))"
    with WindowsJob(memory_bytes=128 * 1024 * 1024, cpu_rate=10000, process_limit=4) as job:
        process = job.spawn([sys.executable, "-c", script, str(output), 'a b " c', "日本語"], cwd=tmp_path,
                            environment=environment(), log_path=tmp_path / "worker.log")
        assert job.pids() == [process.pid] and process.creation_time > 0
        assert not output.exists() and process.wait(20) is None
        limits = job.limits()
        assert limits["memory_bytes"] == 128 * 1024 * 1024 and limits["process_limit"] == 4
        assert limits["flags"] & 0x2000 and limits["flags"] & 0x200
        assert not limits["flags"] & (0x800 | 0x1000)  # Neither breakaway flag.
        assert limits["cpu_flags"] == 5 and limits["cpu_rate"] == 10000
        process.resume()
        assert process.wait(10000) == 0
        assert json.loads(output.read_text()) == [['a b " c', "日本語"], None]
        assert (tmp_path / "worker.log").read_text().strip() == "owned log"


def test_job_memory_limit_is_enforced(tmp_path):
    output = tmp_path / "memory.txt"
    script = """import pathlib,sys,ctypes,json
k=ctypes.WinDLL('kernel32',use_last_error=True)
k.VirtualAlloc.argtypes=[ctypes.c_void_p,ctypes.c_size_t,ctypes.c_ulong,ctypes.c_ulong]
k.VirtualAlloc.restype=ctypes.c_void_p
small=k.VirtualAlloc(None,32*1024*1024,0x3000,4)
assert small
large=k.VirtualAlloc(None,256*1024*1024,0x3000,4)
pathlib.Path(sys.argv[1]).write_text(json.dumps({'small':bool(small),'large':bool(large),'error':ctypes.get_last_error()}))
"""
    with WindowsJob(memory_bytes=128 * 1024 * 1024, cpu_rate=10000, process_limit=4) as job:
        process = job.spawn([sys.executable, "-c", script, str(output)], cwd=tmp_path, environment=environment())
        process.resume()
        assert process.wait(10000) == 0
        result = json.loads(output.read_text())
        assert result["small"] and not result["large"] and result["error"] != 0
        # The kernel's peak charge counter can include the denied request.
        assert job.limits()["memory_bytes"] == 128 * 1024 * 1024


def test_cpu_hard_cap_throttles_real_work(tmp_path):
    output = tmp_path / "cpu.json"
    script = "import time,pathlib,sys,json\na=time.monotonic(); b=time.process_time()\nwhile time.monotonic()-a<2: pass\npathlib.Path(sys.argv[1]).write_text(json.dumps([time.monotonic()-a,time.process_time()-b]))"
    with WindowsJob(memory_bytes=128 * 1024 * 1024, cpu_rate=max(1, 2500 // os.cpu_count()), process_limit=4) as job:
        process = job.spawn([sys.executable, "-c", script, str(output)], cwd=tmp_path, environment=environment())
        process.resume()
        assert process.wait(15000) == 0
        wall, cpu = json.loads(output.read_text())
        assert cpu < wall * 0.6


def test_descendant_survives_parent_then_dies_with_job_and_cannot_break_away(tmp_path):
    output = tmp_path / "child.json"
    script = """import subprocess,sys,pathlib,json
try:
 subprocess.Popen([sys.executable,'-c','pass'],creationflags=0x01000000)
 raise AssertionError('escaped job')
except PermissionError: pass
child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(300)'])
pathlib.Path(sys.argv[1]).write_text(json.dumps(child.pid))
"""
    with WindowsJob(memory_bytes=128 * 1024 * 1024, cpu_rate=10000, process_limit=4) as job:
        process = job.spawn([sys.executable, "-c", script, str(output)], cwd=tmp_path, environment=environment())
        process.resume()
        assert process.wait(10000) == 0
        pid = json.loads(output.read_text())
        assert pid in job.pids()
        kernel, handle = child_handle(pid)
        try:
            job.terminate()
            assert job.wait_empty(10000)
            assert kernel.WaitForSingleObject(handle, 10000) == 0
        finally:
            kernel.CloseHandle(handle)


def test_abrupt_supervisor_death_closes_job_and_kills_worker(tmp_path):
    pid_file, stop = tmp_path / "pid", tmp_path / "stop"
    source = Path(__file__).parents[1] / "src"
    script = """import os,pathlib,sys,time
from task_worker_api.windows_job import WindowsJob
job=WindowsJob(memory_bytes=128*1024*1024,cpu_rate=10000,process_limit=4)
child=job.spawn([sys.executable,'-c','import time; time.sleep(300)'],cwd=sys.argv[3],environment={'SystemRoot':os.environ['SystemRoot']})
child.resume()
pathlib.Path(sys.argv[1]).write_text(str(child.pid))
while not pathlib.Path(sys.argv[2]).exists(): time.sleep(.05)
os._exit(0)
"""
    supervisor = subprocess.Popen([sys.executable, "-c", script, str(pid_file), str(stop), str(tmp_path)],
        env={**environment(), "PYTHONPATH": str(source)}, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        await_file(pid_file)
        kernel, handle = child_handle(int(pid_file.read_text()))
        try:
            stop.touch()
            assert supervisor.wait(10) == 0
            assert kernel.WaitForSingleObject(handle, 10000) == 0
        finally:
            kernel.CloseHandle(handle)
    finally:
        if supervisor.poll() is None:
            supervisor.kill()
            supervisor.wait()


def test_process_count_limit_contains_forks(tmp_path):
    output = tmp_path / "forks.json"
    script = """import subprocess,sys,pathlib,json,time
pids=[]
for index in range(10):
 try: pids.append(subprocess.Popen([sys.executable,'-c',
  'import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text("executed"); time.sleep(300)',
  str(pathlib.Path(sys.argv[1]).parent/('child-'+str(index)))]).pid)
 except OSError as exc:
  pathlib.Path(sys.argv[1]).write_text(json.dumps({'pids':pids,'error':exc.winerror}))
  break
time.sleep(300)
"""
    with WindowsJob(memory_bytes=256 * 1024 * 1024, cpu_rate=10000, process_limit=4) as job:
        process = job.spawn([sys.executable, "-c", script, str(output)], cwd=tmp_path, environment=environment())
        process.resume()
        await_file(output)
        result = json.loads(output.read_text())
        assert len(result["pids"]) == 3 and result["error"]
        expected = {process.pid, *result["pids"]}
        for index in range(3):
            await_file(tmp_path / f"child-{index}")
        assert not (tmp_path / "child-3").exists()
        listed = set(job.pids())
        assert expected <= listed
        handles = []
        try:
            for pid in listed:
                kernel, handle = child_handle(pid)
                handles.append(handle)
                if pid not in expected:
                    # Windows can include its console host outside the worker count.
                    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                        wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
                    name, size = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
                    assert kernel.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size))
                    assert Path(name.value) == Path(os.environ["SystemRoot"]) / "System32" / "conhost.exe"
            job.terminate()
            assert job.wait_empty(10000)
            assert all(kernel.WaitForSingleObject(handle, 10000) == 0 for handle in handles)
        finally:
            for handle in handles:
                kernel.CloseHandle(handle)


def test_reconcile_identity_never_terminates_a_reused_pid_or_wrong_boot(tmp_path):
    host = uuid4()
    boot = physical_boot_id(host, Path('/proc'))
    with WindowsJob(memory_bytes=128*1024*1024, cpu_rate=10000, process_limit=4) as job:
        process = job.spawn([sys.executable, '-c', 'import time; time.sleep(300)'],
            cwd=tmp_path, environment=environment())
        process.resume()
        identity = dict(host_id=host, boot_id=boot, pid=process.pid,
                        creation_time=process.creation_time, timeout_ms=0)
        assert not reconcile_windows_process(**identity)
        assert reconcile_windows_process(**{**identity, 'creation_time': process.creation_time + 1}, terminate=True)
        assert process.wait(0) is None
        with pytest.raises(AdmissionError, match='attempt_fenced'):
            reconcile_windows_process(**{**identity, 'boot_id': uuid4()}, terminate=True)
        assert process.wait(0) is None
        assert reconcile_windows_process(**{**identity, 'timeout_ms': 10000}, terminate=True)
        assert process.wait(0) == 1
        assert reconcile_windows_process(**identity)  # Idempotent after exit.


def test_reconcile_access_denied_is_not_process_exit(tmp_path):
    host = uuid4()
    boot = physical_boot_id(host, Path('/proc'))
    with WindowsJob(memory_bytes=128*1024*1024, cpu_rate=10000, process_limit=4) as job:
        process = job.spawn([sys.executable, '-c', 'import time; time.sleep(300)'],
            cwd=tmp_path, environment=environment())
        process.resume()
        advapi = ctypes.WinDLL('advapi32', use_last_error=True)
        advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR,
            wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
        advapi.SetKernelObjectSecurity.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p]
        descriptor = ctypes.c_void_p()
        # Only this disposable child: deny new handles, preserve the creator's handle.
        assert advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            'D:(D;;GA;;;WD)', 1, ctypes.byref(descriptor), None)
        try:
            assert advapi.SetKernelObjectSecurity(process.handle, 4, descriptor)
        finally:
            job.kernel.LocalFree.argtypes = [ctypes.c_void_p]
            job.kernel.LocalFree(descriptor)
        with pytest.raises(OSError) as failure:
            reconcile_windows_process(host_id=host, boot_id=boot, pid=process.pid,
                creation_time=process.creation_time, timeout_ms=0, terminate=True)
        assert failure.value.winerror == 5
        assert process.wait(0) is None
        job.terminate()
        assert process.wait(10000) is not None


def test_job_cleanup_waits_for_descendants_and_accounts_for_short_lived_children(tmp_path):
    output = tmp_path/'children.json'
    script = '''import json,pathlib,subprocess,sys
for _ in range(12): subprocess.run([sys.executable,'-c','pass'],check=True)
child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(300)'])
pathlib.Path(sys.argv[1]).write_text(json.dumps(child.pid))
'''
    with WindowsJob(memory_bytes=256*1024**2, cpu_rate=10000, process_limit=32) as job:
        root = job.spawn([sys.executable,'-c',script,str(output)], cwd=tmp_path, environment=environment())
        root.resume()
        assert root.wait(10000) == 0
        pid = json.loads(output.read_text())
        kernel, handle = child_handle(pid)
        try:
            assert kernel.WaitForSingleObject(handle, 0) == 258
            assert job.wait_stopped(10000), job.observed
            assert kernel.WaitForSingleObject(handle, 0) == 0
            assert len(job.observed) >= 14
            assert all(record['exited'] for record in job.observed.values())
            assert job.wait_stopped(1000)
            with pytest.raises(RuntimeError, match='stopping'):
                job.spawn([sys.executable,'-c','pass'], cwd=tmp_path, environment=environment())
        finally:
            kernel.CloseHandle(handle)


def test_missing_process_notification_keeps_tree_cleanup_unverified(tmp_path, monkeypatch):
    with WindowsJob(memory_bytes=128*1024**2, cpu_rate=10000, process_limit=8) as job:
        root = job.spawn([sys.executable,'-c','import time; time.sleep(300)'],
                         cwd=tmp_path, environment=environment())
        root.resume()
        observe = job._observe_process
        monkeypatch.setattr(job, '_observe_process', lambda pid: None if pid == root.pid else observe(pid))
        assert not job.wait_stopped(100)
        assert root.wait(10000) is not None
        assert job.pids() == []
        assert root.pid not in job.observed
