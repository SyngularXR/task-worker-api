import json
import logging
from time import monotonic
from types import SimpleNamespace

import pytest

from task_worker_api.admission_supervisor import _log_resources


@pytest.mark.asyncio
async def test_observations_exclude_claim_secrets_and_fail_without_interrupting(caplog):
    claim = SimpleNamespace(task_id=7, ownership=SimpleNamespace(attempt_id='attempt', token='secret-token'),
                            profile=SimpleNamespace(profile_id='profile', revision=1), task={'patient': 'private'})
    snapshot = {'host_id': 'host', 'gpus': {'GPU-test': {'available': 18000, 'allocatable': 24000}}}

    async def read_report():
        return SimpleNamespace(report=SimpleNamespace(model_dump=lambda **kwargs: snapshot))

    with caplog.at_level(logging.INFO):
        await _log_resources(claim, read_report, 'released', monotonic())
    record = json.loads(caplog.records[-1].getMessage().split(' ', 1)[1])
    assert record['event'] == 'released' and record['observation'] == snapshot
    assert 'secret-token' not in caplog.text and 'patient' not in caplog.text

    async def unavailable():
        raise OSError('sensitive path')

    await _log_resources(claim, unavailable, 'active', monotonic())
    assert caplog.records[-1].getMessage() == 'Admission resource observation unavailable'
    assert 'sensitive path' not in caplog.text
