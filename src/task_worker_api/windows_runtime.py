"""Build-time adjustment for a separate, private CPython Windows runtime."""
import hashlib
import json
from pathlib import Path
import sys


def patch_asyncio_pipe_namespace(runtime):
    """Keep CPython's transport, but create its pipes in AppContainer's namespace.

    CPython exposes no pipe-name option. Patch only the copied runtime, before
    enrollment; never change the interpreter running this installer.
    """
    runtime = Path(runtime).resolve(strict=True)
    path = runtime/'Lib/asyncio/windows_utils.py'
    if (runtime == Path(sys.base_prefix).resolve() or not (runtime/'python.exe').is_file()
            or path.resolve(strict=True) != path):
        raise ValueError('A separate private Windows runtime is required')
    source = path.read_bytes()
    old = rb'\\.\pipe\python-pipe-'
    new = rb'\\.\pipe\LOCAL\python-pipe-'
    if source.count(new) == 1 and old not in source:
        return  # Already prepared; preserve its original patch record.
    if source.count(old) != 1 or new in source:
        raise ValueError('Unrecognized CPython pipe implementation; verify before deployment')
    patched = source.replace(old, new)
    path.write_bytes(patched)
    (runtime/'asyncio-pipe-patch.json').write_text(json.dumps(dict(
        path=str(path.relative_to(runtime)), original_sha256=hashlib.sha256(source).hexdigest(),
        patched_sha256=hashlib.sha256(patched).hexdigest()), indent=2))
