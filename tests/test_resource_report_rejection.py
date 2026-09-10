from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest

from task_worker_api.client import BackendClient
from task_worker_api.claim_journal import ClaimJournal


@pytest.mark.asyncio
@pytest.mark.parametrize("code,rejected", [
    ("hardware_report_replayed", True),
    ("hardware_report_stale", True),
    ("cleanup_evidence_stale", True),
    ("attempt_fenced", False),
    ("idempotency_conflict", False),
])
async def test_report_rejection_allows_fresh_evidence_but_preserves_uncertain_operations(code, rejected):
    response = httpx.Response(409, json={"code": code}, request=httpx.Request("POST", "http://test/release"))
    client = object.__new__(BackendClient)
    client._resource_request = AsyncMock(side_effect=httpx.HTTPStatusError("rejected", request=response.request, response=response))
    journal = Mock()
    journal.operation_request.return_value = {}
    operation_id = uuid4()
    with pytest.raises(httpx.HTTPStatusError):
        await client._resource_replay_operation(journal, SimpleNamespace(task_id=1), "release", operation_id)
    if rejected:
        journal.record_operation.assert_called_once_with("release", operation_id, {"rejected": code})
    else:
        journal.record_operation.assert_not_called()


def test_replayed_report_rejection_rotates_durable_request(tmp_path):
    journal = ClaimJournal(tmp_path / "claim.sqlite")
    claim = journal.prepare(uuid4(), frozenset({"test"}))
    journal.record_response(claim.claim_request_id, SimpleNamespace(model_dump=lambda **_: {"task_id": 1}))
    old = journal.prepare_operation("release", {}, request={"sequence": 1})
    journal.record_operation("release", old, {"rejected": "hardware_report_replayed"})
    new = journal.prepare_operation("release", {}, request={"sequence": 2})
    assert new != old
    assert journal.operation_request("release", new)["sequence"] == 2
