import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from task_worker_api import admitted_worker
from task_worker_api.claim_journal import ClaimJournal
from task_worker_api.enums import TaskType
from task_worker_api.resources import AdmissionError
from .test_resources import _admitted_journal


def _config(tmp_path, journal_path, credential_path, handlers):
    return dict(
        backend_url="http://test", worker_id="test-worker",
        journal_file=str(journal_path), credential_file=str(credential_path),
        report_file=str(tmp_path / "report.json"), work_dir=str(tmp_path),
        handlers=handlers,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["empty", "unanswered", "recorded_null"])
async def test_run_refuses_without_recorded_claim(tmp_path, monkeypatch, state):
    journal = ClaimJournal(tmp_path / "claim.sqlite")
    if state != "empty":
        request = journal.prepare(uuid4(), frozenset({"render"}))
        if state == "recorded_null":
            journal.record_response(request.claim_request_id, None)

    constructed = []
    monkeypatch.setattr(admitted_worker, "Worker", lambda **kwargs: constructed.append(kwargs))
    config = _config(tmp_path, journal.path, tmp_path / "missing-credential", {})

    with pytest.raises(AdmissionError, match="^claim_request_unknown$"):
        await admitted_worker.run(config)

    assert constructed == []


@pytest.fixture
def fake_worker(monkeypatch):
    class FakeWorker:
        instances = []
        error = RuntimeError("sentinel attempt failure")

        def __init__(self, **kwargs):
            self.options = kwargs
            self._client = SimpleNamespace(close=AsyncMock())
            self.run_admitted_attempt = AsyncMock(side_effect=self.error)
            self.instances.append(self)

    monkeypatch.setattr(admitted_worker, "Worker", FakeWorker)
    return FakeWorker


@pytest.mark.asyncio
async def test_run_routes_handler_through_invoke(tmp_path, monkeypatch, fake_worker):
    journal, _ = _admitted_journal(tmp_path, uuid4())
    credential = tmp_path / "credential"
    credential.write_text("synthetic-key\n")
    module_name = "admitted_handler_" + uuid4().hex
    (tmp_path / (module_name + ".py")).write_text(
        "async def selected(ctx, params):\n    return ctx, params\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    config = _config(tmp_path, journal.path, credential, {"render": module_name + ":selected"})

    with pytest.raises(RuntimeError) as raised:
        await admitted_worker.run(config)
    assert raised.value is fake_worker.error
    worker, = fake_worker.instances
    assert worker.options["api_key"] == "synthetic-key"
    assert worker.options["foreign_targets"] == []

    ctx, params = object(), {"original": object()}
    try:
        received_ctx, received_params = await worker.options["handlers"][TaskType.RENDER](ctx, params)
    finally:
        sys.modules.pop(module_name, None)
    assert received_ctx is ctx
    assert received_params is params


@pytest.mark.asyncio
async def test_run_closes_client_when_attempt_raises(tmp_path, fake_worker):
    journal, claim = _admitted_journal(tmp_path, uuid4())
    credential = tmp_path / "credential"
    credential.write_text("synthetic-key\n")
    config = _config(tmp_path, journal.path, credential, {})

    with pytest.raises(RuntimeError) as raised:
        await admitted_worker.run(config)

    assert raised.value is fake_worker.error
    worker, = fake_worker.instances
    worker.run_admitted_attempt.assert_awaited_once()
    actual_claim, actual_journal, _ = worker.run_admitted_attempt.await_args.args
    assert actual_claim == claim
    assert actual_journal.path == journal.path
    worker._client.close.assert_awaited_once()
