import json
import inspect
import os
import socket
from uuid import uuid4

import pytest

from task_worker_api.windows_job import WindowsAppContainer, WindowsJob
from task_worker_api.files import _canonical_output_path
from .windows_helpers import stage_python, grant_access

pytestmark = pytest.mark.skipif(os.name != "nt", reason="real Windows AppContainer check")


@pytest.mark.parametrize("network", [False, True])
def test_appcontainer_denies_supervisor_access_and_preserves_staged_permissions(tmp_path, network):
    runtime, work = tmp_path / "runtime", tmp_path / "work"
    stage_python(runtime)
    work.mkdir()
    secret = tmp_path / "supervisor-secret"
    secret.write_text("must remain private")
    immutable = runtime / "input.txt"
    immutable.write_text("frozen input")
    output = work / "result.json"
    script = 'from pathlib import Path\nimport os\n' + inspect.getsource(_canonical_output_path) + r'''
import ctypes,json,pathlib,sys,subprocess,socket
from ctypes import wintypes as w
k=ctypes.WinDLL('kernel32',use_last_error=True)
a=ctypes.WinDLL('advapi32',use_last_error=True)
k.GetCurrentProcess.restype=w.HANDLE
k.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD]
k.OpenProcess.restype=w.HANDLE
k.CloseHandle.argtypes=[w.HANDLE]
a.OpenProcessToken.argtypes=[w.HANDLE,w.DWORD,ctypes.POINTER(w.HANDLE)]
a.GetTokenInformation.argtypes=[w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD,ctypes.POINTER(w.DWORD)]
token=w.HANDLE(); flag=w.DWORD(); size=w.DWORD()
assert a.OpenProcessToken(k.GetCurrentProcess(),8,ctypes.byref(token))
assert a.GetTokenInformation(token,29,ctypes.byref(flag),4,ctypes.byref(size))
a.GetTokenInformation(token,30,None,0,ctypes.byref(size))
capabilities=ctypes.create_string_buffer(size.value)
assert a.GetTokenInformation(token,30,capabilities,size.value,ctypes.byref(size))
k.CloseHandle(token)
result={'app_container':flag.value,'capability_count':w.DWORD.from_buffer(capabilities).value,
 'input':pathlib.Path(sys.argv[2]).read_text()}
probe=Path(sys.argv[1]).with_name('output.txt')
probe.write_text('owned output')
assert _canonical_output_path(probe).is_relative_to(_canonical_output_path(probe.parent))
assert not _canonical_output_path(Path(sys.argv[2])).is_relative_to(_canonical_output_path(probe.parent))
for key,path,mode in [('secret',sys.argv[3],'r'),('immutable',sys.argv[2],'w')]:
 try:
  with open(path,mode): pass
  result[key]='allowed'
 except PermissionError: result[key]='denied'
handle=k.OpenProcess(0x40,False,int(sys.argv[4]))
result['parent_duplicate_handle']=bool(handle)
result['parent_error']=ctypes.get_last_error()
if handle: k.CloseHandle(handle)
descendant=subprocess.run([sys.executable,'-I','-S','-c',
 'import sys;\ntry: open(sys.argv[1]); sys.exit(1)\nexcept PermissionError: sys.exit(0)',sys.argv[3]])
result['descendant_denied']=descendant.returncode == 0
import asyncio
async def pipe_child():
 child=await asyncio.create_subprocess_exec(sys.executable,'-I','-S','-c',
  'import sys; print("stdout"); print("stderr",file=sys.stderr)',
  stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
 out,err=await child.communicate()
 assert child.returncode == 0 and out.strip()==b'stdout' and err.strip()==b'stderr'
asyncio.run(pipe_child())
with socket.socket() as connection:
 connection.settimeout(2)
 try:
  connection.connect(('127.0.0.1',int(sys.argv[5])))
  result['network']='allowed'
 except OSError as exc: result['network']=type(exc).__name__
pathlib.Path(sys.argv[1]).write_text(json.dumps(result))
'''
    with socket.socket() as listener, WindowsAppContainer("surgiclaw." + uuid4().hex, network=network) as app:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        with socket.create_connection(listener.getsockname(), timeout=2):
            peer, _ = listener.accept()
            peer.close()
        with pytest.raises(OSError, match="800700b7"):
            WindowsAppContainer(app.name)  # Never adopt an existing security identity.
        for path, access in ((runtime, "RX"), (work, "M")):
            grant_access(app, path, access)
        with WindowsJob(memory_bytes=256*1024*1024, cpu_rate=10000, process_limit=8) as job:
            child = job.spawn([str(runtime / "python.exe"), "-I", "-S", "-c", script,
                str(output), str(immutable), str(secret), str(os.getpid()), str(listener.getsockname()[1])], cwd=work,
                environment={"SystemRoot": os.environ["SystemRoot"], "LOCALAPPDATA": os.environ["LOCALAPPDATA"],
                    "TEMP": str(work), "TMP": str(work)},
                log_path=tmp_path / "worker.log", app_container=app)
            child.resume()
            assert child.wait(15000) == 0, (tmp_path / "worker.log").read_text()
            assert job.wait_empty(10000)
        result = json.loads(output.read_text())
        # Windows filtering can drop the connection or return access denied.
        assert result.pop('network') in ('TimeoutError', 'PermissionError')
        assert result == dict(app_container=1, capability_count=2 if network else 0, input="frozen input",
            secret="denied", immutable="denied", parent_duplicate_handle=False, parent_error=5,
            descendant_denied=True)
        assert immutable.read_text() == "frozen input"
