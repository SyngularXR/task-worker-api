import base64
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from task_worker_api import TaskType
from task_worker_api.client import BackendClient
from task_worker_api.errors import ProtocolError
from task_worker_api.resource_execution import AttemptLease
from task_worker_api.schemas import (
    TASK_PARAMS_SCHEMAS,
    InteractiveRound,
    InteractiveRoundReply,
    InteractiveSessionParams,
)

from .test_resource_execution import Client

DIMS = [8, 6, 4]


def plane(axis, slice_=1):
    return {"dims": DIMS, "axis": axis, "depth_range": [0, 3],
            "seeds": [{"slice": slice_, "obj_id": 1, "points": [[1, 1]], "point_labels": [1]}]}


def test_session_is_a_registered_task_type():
    assert TASK_PARAMS_SCHEMAS[TaskType.INTERACTIVE_SESSION] is InteractiveSessionParams
    assert len(TaskType.INTERACTIVE_SESSION.value) <= 20  # backend task_type column
    params = InteractiveSessionParams(input_path="in.nii.gz", output_dir="out", case_id=1, dicom_id=2,
                                      dims=DIMS, modality="mri")
    assert params.idle_seconds == 300
    with pytest.raises(ValidationError):
        InteractiveSessionParams(**{**params.model_dump(), "idle_seconds": 3600})


def test_a_round_carries_every_plane_once():
    round_ = InteractiveRound(round_id=1, kind="propagate", planes=[plane(0), plane(1), plane(2)])
    assert [p.axis for p in round_.planes] == [0, 1, 2]
    with pytest.raises(ValidationError, match="once"):
        InteractiveRound(round_id=2, kind="propagate", planes=[plane(0), plane(0, 2)])
    with pytest.raises(ValidationError, match="dimensions"):
        InteractiveRound(round_id=2, kind="propagate", planes=[plane(0), {**plane(1), "dims": [8, 6, 5]}])


def test_click_and_close_rounds():
    click = {"dims": DIMS, "axis": 1, "slice": 5, "obj_id": 2, "points": [[7, 3]], "point_labels": [1]}
    assert InteractiveRound(round_id=3, kind="click", click=click).click.slice == 5
    with pytest.raises(ValidationError, match="outside the volume"):
        InteractiveRound(round_id=3, kind="click", click={**click, "slice": 6})  # coronal depth = y = 6
    with pytest.raises(ValidationError, match="outside the slice"):
        InteractiveRound(round_id=3, kind="click", click={**click, "points": [[8, 0]]})
    assert InteractiveRound(round_id=4, kind="close").planes is None
    with pytest.raises(ValidationError, match="planes to propagate"):
        InteractiveRound(round_id=4, kind="close", planes=[plane(0)])
    with pytest.raises(ValidationError, match="planes to propagate"):
        InteractiveRound(round_id=4, kind="propagate")


def test_a_failed_reply_has_an_error_and_no_files():
    assert InteractiveRoundReply(round_id=1, status="done", files={"axis_0.gz": "AA=="}).files
    with pytest.raises(ValidationError):
        InteractiveRoundReply(round_id=1, status="failed", files={"axis_0.gz": "AA=="}, error="x")
    with pytest.raises(ValidationError):
        InteractiveRoundReply(round_id=1, status="failed")


def _claim():
    return SimpleNamespace(task_id=9, ownership=SimpleNamespace(
        attempt_id=uuid4(), token="secret",
        model_dump=lambda mode, exclude: {"worker_instance_id": "w", "generation": 1}))


@pytest.mark.asyncio
async def test_client_long_polls_rounds_and_sends_replies():
    claim = _claim()
    seen = []
    answers = iter([httpx.Response(204), httpx.Response(200, json={"round_id": 1, "kind": "close"})])

    def handler(request):
        seen.append(request)
        return next(answers) if request.method == "GET" else httpx.Response(200, json={"status": "ok"})

    client = BackendClient("http://backend/api/v1", "k", client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://backend/api/v1"))
    try:
        assert await client.resource_round_next(claim, 20) is None
        assert (await client.resource_round_next(claim, 20)).kind == "close"
        with pytest.raises(ValueError):
            await client.resource_round_next(claim, 300)
        files = {"axis_0.gz": base64.b64encode(b"x").decode()}
        await client.resource_round_reply(claim, {"round_id": 1, "status": "done", "files": files})
        with pytest.raises(ProtocolError):
            await client.resource_round_reply(claim, {"round_id": 1, "status": "done", "files": {"../x": "AA=="}})
    finally:
        await client.close()
    get, _, put = seen
    assert get.url.path == f"/api/v1/tasks/9/attempts/{claim.ownership.attempt_id}/rounds/next"
    assert get.headers["X-Attempt-Token"] == "secret" and get.url.params["wait_s"] == "20"
    assert put.url.path.endswith("/rounds/1/reply") and json.loads(put.content)["files"] == files


@pytest.mark.asyncio
async def test_lease_rounds_need_a_start_and_a_409_ends_the_lease():
    client = Client()

    async def gone(claim, wait_s):
        raise httpx.HTTPStatusError("fenced", request=httpx.Request("GET", "http://x"),
                                    response=httpx.Response(409))

    client.resource_round_next = gone
    async with AttemptLease(client, None, client.claim) as lease:
        assert lease.execution_remaining_seconds == 0.0
        with pytest.raises(ProtocolError, match="live acknowledged start"):
            await lease.next_round()
        await lease.start(None, "staged-inputs")
        assert 0 < lease.execution_remaining_seconds <= 10
        with pytest.raises(httpx.HTTPStatusError):
            await lease.next_round()
        assert lease.is_cancelled and lease.execution_remaining_seconds == 0.0
