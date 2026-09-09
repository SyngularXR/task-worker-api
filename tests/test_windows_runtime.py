import json
from pathlib import Path
import sys

import pytest

from task_worker_api.windows_runtime import patch_asyncio_pipe_namespace


def test_private_runtime_patch_is_idempotent_and_rejects_unknown_source(tmp_path):
    (tmp_path/'python.exe').touch()
    source = tmp_path/'Lib/asyncio/windows_utils.py'
    source.parent.mkdir(parents=True)
    source.write_bytes(rb"address = r'\\.\pipe\python-pipe-example'")
    patch_asyncio_pipe_namespace(tmp_path)
    record = json.loads((tmp_path/'asyncio-pipe-patch.json').read_text())
    assert record['original_sha256'] != record['patched_sha256']
    assert rb'\\.\pipe\LOCAL\python-pipe-example' in source.read_bytes()
    patch_asyncio_pipe_namespace(tmp_path)
    assert json.loads((tmp_path/'asyncio-pipe-patch.json').read_text()) == record
    source.write_bytes(b'unknown implementation')
    with pytest.raises(ValueError, match='Unrecognized'):
        patch_asyncio_pipe_namespace(tmp_path)
    assert source.read_bytes() == b'unknown implementation'
    with pytest.raises(ValueError, match='separate private'):
        patch_asyncio_pipe_namespace(Path(sys.base_prefix))
