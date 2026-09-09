import ctypes
from ctypes import wintypes as w
import os
from pathlib import Path
import shutil
import subprocess
import sys

from task_worker_api.windows_runtime import patch_asyncio_pipe_namespace


def stage_python(runtime):
    runtime.mkdir()
    base = Path(sys.base_prefix)
    shutil.copy2(base / 'python.exe', runtime)
    for dll in base.glob('*.dll'):
        shutil.copy2(dll, runtime)
    shutil.copytree(base / 'DLLs', runtime / 'DLLs')
    shutil.copytree(base / 'Lib', runtime / 'Lib',
                    ignore=shutil.ignore_patterns('site-packages', '__pycache__', 'test', 'tests'))
    patch_asyncio_pipe_namespace(runtime)


def grant_access(app, path, access):
    sid = w.LPWSTR()
    app.advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(w.LPWSTR)]
    assert app.advapi.ConvertSidToStringSidW(app.sid, ctypes.byref(sid))
    try:
        subprocess.run([str(Path(os.environ['SystemRoot']) / 'System32/icacls.exe'), str(path),
            '/grant', f'*{sid.value}:(OI)(CI)({access})', '/T', '/Q'], check=True,
            capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    finally:
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree(sid)
