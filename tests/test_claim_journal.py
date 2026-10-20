"""Direct claim-response refusals preserve durable worker journal state."""
from uuid import uuid4

import pytest

from task_worker_api.claim_journal import ClaimJournal
from task_worker_api.resources import AdmissionError

from .test_resources import _admitted_claim, _admitted_journal


def _assert_refused(journal, request_id, result, code):
    pending = journal.pending()
    operations = journal.unresolved_operations()
    with pytest.raises(AdmissionError) as caught:
        journal.record_response(request_id, result)
    assert caught.value.code == code
    assert str(caught.value) == code
    assert journal.pending() == pending
    assert journal.unresolved_operations() == operations
    reopened = ClaimJournal(journal.path)
    assert reopened.pending() == pending
    assert reopened.unresolved_operations() == operations


@pytest.mark.parametrize("stored", ["absent", "unanswered", "claim"])
def test_record_response_refuses_unknown_request(tmp_path, stored):
    instance = uuid4()
    if stored == "claim":
        journal, claim = _admitted_journal(tmp_path, instance)
        operation_id = journal.prepare_operation("start", {}, request={})
        assert journal.unresolved_operations() == [("start", operation_id)]
    else:
        journal = ClaimJournal(tmp_path / "claim.sqlite")
        claim = _admitted_claim(instance)
        if stored == "unanswered":
            journal.prepare(instance, frozenset({"finalize_segment"}))
    _assert_refused(journal, uuid4(), claim, "claim_request_unknown")


@pytest.mark.parametrize("field", ["execution_scope", "input_digest", "ownership"])
def test_record_response_refuses_changed_immutable_claim(tmp_path, field):
    journal, claim = _admitted_journal(tmp_path, uuid4())
    operation_id = journal.prepare_operation("start", {}, request={})
    assert journal.unresolved_operations() == [("start", operation_id)]
    changes = {
        "execution_scope": "other-scope",
        "input_digest": "b" * 64,
        "ownership": claim.ownership.model_copy(update={"attempt_id": uuid4()}),
    }
    changed = claim.model_copy(update={field: changes[field]})
    request, _ = journal.pending()
    _assert_refused(journal, request.claim_request_id, changed, "idempotency_conflict")


def test_record_response_refuses_null_to_claim(tmp_path):
    instance = uuid4()
    journal = ClaimJournal(tmp_path / "claim.sqlite")
    request = journal.prepare(instance, frozenset({"finalize_segment"}))
    journal.record_response(request.claim_request_id, None)
    _assert_refused(journal, request.claim_request_id, _admitted_claim(instance),
                    "idempotency_conflict")


def test_record_response_refuses_claim_to_null(tmp_path):
    journal, _ = _admitted_journal(tmp_path, uuid4())
    operation_id = journal.prepare_operation("start", {}, request={})
    assert journal.unresolved_operations() == [("start", operation_id)]
    request, _ = journal.pending()
    _assert_refused(journal, request.claim_request_id, None, "idempotency_conflict")
