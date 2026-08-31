"""Cross-box worker sharing: SYNPUSHER_TARGETS parsing, home-first polling,
per-target backoff, per-task client binding, the foreign local-mode guard,
output-mode rules, and the volume-affinity check.

Design: SynPusher-Vue docs/specs/2026-08-27-cross-box-worker-sharing-design.md
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from task_worker_api import (
    CrossBoxLocalModeError,
    ForeignTarget,
    ProtocolError,
    TaskContext,
    TaskType,
    Worker,
    parse_synpusher_targets,
)
from task_worker_api.context import ClaimedTask, FileContext
from task_worker_api.enums import TaskStatus
from task_worker_api.files import (
    _require_foreign_capable,
    prepare_inputs,
    upload_outputs,
)
from task_worker_api.testing import FakeBackendClient


def _mi_task(params: dict, task_id: int = 1) -> ClaimedTask:
    return ClaimedTask(
        id=task_id,
        task_type=TaskType.MODEL_INITIALIZING,
        case_id=1,
        item_key="m",
        status=TaskStatus.PENDING,
        params=params,
    )


def _gs_task(params: dict, task_id: int = 2) -> ClaimedTask:
    return ClaimedTask(
        id=task_id,
        task_type=TaskType.GS_BUILD,
        case_id=1,
        item_key="aura",
        status=TaskStatus.PENDING,
        params=params,
    )


def _foreign(fake: FakeBackendClient, types=None) -> ForeignTarget:
    return ForeignTarget(
        url="http://foreign-box:5000/api/v1",
        api_key="foreign-key",
        task_types=types or [TaskType.MODEL_INITIALIZING],
        client=fake,
    )


async def _mi_handler(ctx: TaskContext, params) -> dict:
    out = ctx.files.output_dir / "hull.glb"
    out.write_bytes(b"glb-bytes")
    return {"primary": ctx.files.primary_path.name,
            "output_files": {"hull": "hull.glb"}}


# ---------------------------------------------------------------------------
# parse_synpusher_targets
# ---------------------------------------------------------------------------


def test_parse_targets_valid_multi():
    targets = parse_synpusher_targets(
        "http://a/api/v1|key-a|model_initializing,cinematic_baking;"
        "http://b/api/v1|key-b|model_initializing"
    )
    assert [t.url for t in targets] == ["http://a/api/v1", "http://b/api/v1"]
    assert targets[0].task_types == [
        TaskType.MODEL_INITIALIZING, TaskType.CINEMATIC_BAKING,
    ]
    assert targets[1].api_key == "key-b"


def test_parse_targets_empty_and_none():
    assert parse_synpusher_targets(None) == []
    assert parse_synpusher_targets("") == []
    assert parse_synpusher_targets("  ;  ") == []


@pytest.mark.parametrize("raw", [
    "http://a|key-a",                    # missing types field
    "http://a|key-a|",                   # empty types field
    "http://a||model_initializing",      # empty key
    "|key|model_initializing",           # empty url
    "http://a|key-a|not_a_type",         # unknown task type
])
def test_parse_targets_malformed_fail_fast(raw):
    with pytest.raises(ProtocolError):
        parse_synpusher_targets(raw)


# ---------------------------------------------------------------------------
# Worker construction
# ---------------------------------------------------------------------------


def test_worker_rejects_home_url_listed_as_foreign(make_worker):
    with pytest.raises(ProtocolError, match="home box"):
        make_worker(
            handlers={TaskType.MODEL_INITIALIZING: _mi_handler},
            backend_url="http://home:5000/api/v1",
            foreign_targets=[ForeignTarget(
                url="http://home:5000/api/v1/",
                api_key="k",
                task_types=[TaskType.MODEL_INITIALIZING],
                client=FakeBackendClient(),
            )],
        )


def test_worker_rejects_target_with_no_handled_types(make_worker):
    with pytest.raises(ProtocolError, match="no task type this"):
        make_worker(
            handlers={TaskType.MODEL_INITIALIZING: _mi_handler},
            foreign_targets=[_foreign(
                FakeBackendClient(), types=[TaskType.CINEMATIC_BAKING],
            )],
        )


def test_default_worker_id_warns_with_targets(make_worker, caplog):
    with caplog.at_level(logging.WARNING):
        make_worker(
            handlers={TaskType.MODEL_INITIALIZING: _mi_handler},
            worker_id="blender-worker-1",
            foreign_targets=[_foreign(FakeBackendClient())],
        )
    assert any("globally unique" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Home-first polling + per-target backoff
# ---------------------------------------------------------------------------


class _RecordingFake(FakeBackendClient):
    def __init__(self, label: str, record: list):
        super().__init__()
        self._label = label
        self._record = record

    async def claim_next(self, task_types, worker_id):
        self._record.append(self._label)
        return await super().claim_next(task_types, worker_id)


class _DeadFake(FakeBackendClient):
    def __init__(self, label: str, record: list):
        super().__init__()
        self._label = label
        self._record = record

    async def claim_next(self, task_types, worker_id):
        self._record.append(self._label)
        raise ConnectionError("box unreachable")


@pytest.mark.asyncio
async def test_home_polled_first_every_cycle(make_worker, tmp_path):
    record: list[str] = []
    home = _RecordingFake("home", record)
    foreign = _RecordingFake("foreign", record)
    src = tmp_path / "part.stl"
    src.write_bytes(b"solid\n")
    home.queue_task(
        task_type=TaskType.MODEL_INITIALIZING,
        params={"job_id": "j1", "input_path": str(src), "base_name": "part"},
    )
    ftask = foreign.queue_task(
        task_type=TaskType.MODEL_INITIALIZING,
        params={"job_id": "j2", "input_path": "/x.stl", "base_name": "x",
                "input_files": {"x.stl": "x.stl"}},
    )
    foreign.queue_file(ftask.id, "x.stl", b"stl-bytes")

    worker = make_worker(
        client=home,
        handlers={TaskType.MODEL_INITIALIZING: _mi_handler},
        foreign_targets=[_foreign(foreign)],
    )
    # Cycle 1: home has work — foreign must not even be polled.
    assert await worker.run_one() is True
    assert record == ["home"]
    assert len(home.completed_tasks) == 1
    # Cycle 2: home empty — foreign polled after home, its task runs there.
    assert await worker.run_one() is True
    assert record == ["home", "home", "foreign"]
    assert len(foreign.completed_tasks) == 1
    assert len(home.completed_tasks) == 1  # home's own task stayed home


@pytest.mark.asyncio
async def test_dead_foreign_target_backs_off_and_recovers(make_worker):
    record: list[str] = []
    home = _RecordingFake("home", record)
    dead = _DeadFake("dead", record)
    second = _RecordingFake("second", record)

    worker = make_worker(
        client=home,
        handlers={TaskType.MODEL_INITIALIZING: _mi_handler},
        foreign_targets=[
            ForeignTarget(url="http://dead/api/v1", api_key="k1",
                          task_types=[TaskType.MODEL_INITIALIZING], client=dead),
            ForeignTarget(url="http://second/api/v1", api_key="k2",
                          task_types=[TaskType.MODEL_INITIALIZING], client=second),
        ],
    )
    # Cycle 1: dead target raises → backoff armed; second still polled.
    await worker.run_one()
    assert record == ["home", "dead", "second"]
    # Cycles 2-3: dead is skipped (2**1 = 2 cycles), second keeps being polled.
    await worker.run_one()
    await worker.run_one()
    assert record.count("dead") == 1
    assert record.count("second") == 3
    # Cycle 4: backoff expired — dead is retried.
    await worker.run_one()
    assert record.count("dead") == 2


# ---------------------------------------------------------------------------
# Per-task binding + foreign mode rules (worker level)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_foreign_task_all_calls_land_on_foreign(make_worker):
    home = FakeBackendClient()
    foreign = FakeBackendClient()
    task = foreign.queue_task(
        task_type=TaskType.MODEL_INITIALIZING,
        params={"job_id": "j", "input_path": "/app/shared/1/x.stl",
                "base_name": "x", "input_files": {"x.stl": "x.stl"}},
    )
    foreign.queue_file(task.id, "x.stl", b"stl-bytes")

    worker = make_worker(
        client=home,
        handlers={TaskType.MODEL_INITIALIZING: _mi_handler},
        foreign_targets=[_foreign(foreign)],
    )
    assert await worker.run_one() is True

    # Complete + upload + progress all on the foreign box; home untouched.
    assert len(foreign.completed_tasks) == 1
    result = foreign.completed_tasks[0]["result"]
    assert result["primary"] == "x.stl"  # input came via HTTP, not input_path
    assert (task.id, "hull.glb") in foreign.uploaded_files
    assert foreign.uploaded_files[(task.id, "hull.glb")] == b"glb-bytes"
    assert result["output_files"] == {"hull": "hull.glb"}  # filenames, not paths
    assert home.completed_tasks == []
    assert home.uploaded_files == {}
    assert home.progress_events == []
    assert any(e["task_id"] == task.id for e in foreign.progress_events)


@pytest.mark.asyncio
async def test_foreign_local_mode_task_fails_loudly(make_worker, tmp_path):
    """input_path-only task claimed cross-box → refused before any FS access.

    The wrong-file hazard: the path may EXIST on this worker's own volume and
    be another box's patient file. Stage a real decoy at the same path to
    prove it is never read.
    """
    home = FakeBackendClient()
    foreign = FakeBackendClient()
    decoy = tmp_path / "decoy.stl"
    decoy.write_bytes(b"WRONG BOX PATIENT DATA")
    foreign.queue_task(
        task_type=TaskType.MODEL_INITIALIZING,
        params={"job_id": "j", "input_path": str(decoy), "base_name": "d"},
    )

    seen = []

    async def handler(ctx, params):
        seen.append(ctx)
        return {}

    worker = make_worker(
        client=home,
        handlers={TaskType.MODEL_INITIALIZING: handler},
        foreign_targets=[_foreign(foreign)],
    )
    assert await worker.run_one() is True
    assert seen == []  # handler never ran
    assert len(foreign.failed_tasks) == 1
    assert "cross-box claim refused" in foreign.failed_tasks[0]["error"]
    assert home.failed_tasks == []


@pytest.mark.asyncio
async def test_home_dual_param_task_keeps_zero_copy_and_local_publish(
    make_worker, tmp_path,
):
    """[REGRESSION] input_files alongside input_path must not change home
    behavior: input staged from the shared path, output published to the
    shared-volume staging dir — no HTTP transfer in either direction."""
    home = FakeBackendClient()
    src = tmp_path / "part.stl"
    src.write_bytes(b"solid\n")
    shared = tmp_path / "shared"
    shared.mkdir()
    task = home.queue_task(
        task_type=TaskType.MODEL_INITIALIZING,
        params={"job_id": "j", "input_path": str(src), "base_name": "part",
                "input_files": {"part.stl": "part.stl"}},
    )
    # Deliberately NOT calling home.queue_file: a home worker must not
    # download; if it tried, download_file would raise FileNotFoundError.

    worker = make_worker(
        client=home,
        handlers={TaskType.MODEL_INITIALIZING: _mi_handler},
        shared_volume_path=str(shared),
    )
    assert await worker.run_one() is True
    assert len(home.completed_tasks) == 1
    result = home.completed_tasks[0]["result"]
    assert result["primary"] == "part.stl"
    assert home.uploaded_files == {}  # no HTTP output upload
    staged = shared / "temp" / str(task.id) / "hull.glb"
    assert staged.is_file()
    assert result["output_files"] == {"hull": str(staged)}


# ---------------------------------------------------------------------------
# files.py unit level — guard + output-mode rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("params", [
    {"output_dir": "/app/shared/seg/out"},
    {"snapshot_path": "/app/shared/deploy/snap",
     "input_files": {"a": "a"}},
])
def test_require_foreign_capable_refuses_local_only_params(params):
    with pytest.raises(CrossBoxLocalModeError, match="shared-volume-only"):
        _require_foreign_capable(_mi_task(params))


def test_require_foreign_capable_accepts_dual_and_pure_param():
    _require_foreign_capable(_mi_task({"input_path": "/x", "input_files": {"a": "a"}}))
    _require_foreign_capable(_mi_task({"some_knob": 3}))


def test_require_foreign_capable_requires_scene_bundle():
    with pytest.raises(CrossBoxLocalModeError, match="shared-volume-only"):
        _require_foreign_capable(_gs_task({"scene": "/app/shared/grid/gs"}))

    _require_foreign_capable(_gs_task({
        "scene": "/app/shared/grid/gs",
        "input_path": "/app/shared/grid/gs/train_images/000.png",
        "input_files": {"scene": "scene.zip"},
    }))


@pytest.mark.asyncio
async def test_prepare_inputs_foreign_ignores_input_path(tmp_path):
    fake = FakeBackendClient()
    decoy = tmp_path / "x.stl"
    decoy.write_bytes(b"WRONG BOX")
    task = _mi_task({"input_path": str(decoy), "input_files": {"x.stl": "x.stl"}})
    fake.queue_file(task.id, "x.stl", b"RIGHT BOX")
    ctx = await prepare_inputs(task, fake, tmp_path / "work", foreign=True)
    assert ctx.primary_path.read_bytes() == b"RIGHT BOX"


@pytest.mark.asyncio
async def test_upload_outputs_mode_rules(tmp_path):
    """input_files-only stays HTTP (legacy volume-less workers); dual-param
    home stays local; foreign is HTTP regardless of input_path."""
    async def _run(params, *, foreign, shared):
        fake = FakeBackendClient()
        work = tmp_path / f"w_{foreign}_{bool(shared)}_{len(params)}"
        out_dir = work / "out"
        out_dir.mkdir(parents=True)
        (out_dir / "r.bin").write_bytes(b"r")
        ctx = FileContext(input_dir=work, output_dir=out_dir,
                          primary_path=out_dir, all_paths={})
        task = _mi_task(params, task_id=9)
        manifest = await upload_outputs(
            task, fake, ctx, {"r": "r.bin"},
            str(shared) if shared else None, foreign=foreign,
        )
        return fake, manifest

    shared = tmp_path / "sv"
    shared.mkdir()

    fake, manifest = await _run({"input_files": {"r.bin": "r.bin"}},
                                foreign=False, shared=shared)
    assert (9, "r.bin") in fake.uploaded_files          # HTTP: legacy remote
    assert manifest == {"r": "r.bin"}

    fake, manifest = await _run(
        {"input_path": "/x", "input_files": {"r.bin": "r.bin"}},
        foreign=False, shared=shared,
    )
    assert fake.uploaded_files == {}                     # local: home dual-param
    assert manifest["r"].startswith(str(shared))

    fake, manifest = await _run(
        {"input_path": "/x", "input_files": {"r.bin": "r.bin"}},
        foreign=True, shared=shared,
    )
    assert (9, "r.bin") in fake.uploaded_files           # HTTP: foreign always
    assert manifest == {"r": "r.bin"}


# ---------------------------------------------------------------------------
# Volume-affinity check
# ---------------------------------------------------------------------------


class _BoxIdFake(FakeBackendClient):
    def __init__(self, box_id):
        super().__init__()
        self._box_id = box_id

    async def get_box_id(self):
        return self._box_id


def _affinity_worker(make_worker, tmp_path, home_client):
    return make_worker(
        client=home_client,
        handlers={TaskType.MODEL_INITIALIZING: _mi_handler},
        shared_volume_path=str(tmp_path),
        foreign_targets=[_foreign(FakeBackendClient())],
    )


@pytest.mark.asyncio
async def test_affinity_match_passes(make_worker, tmp_path):
    (tmp_path / ".box-id").write_text("box-A\n")
    worker = _affinity_worker(make_worker, tmp_path, _BoxIdFake("box-A"))
    await worker._verify_home_affinity()  # must not raise


@pytest.mark.asyncio
async def test_affinity_mismatch_is_fatal(make_worker, tmp_path):
    (tmp_path / ".box-id").write_text("box-A")
    worker = _affinity_worker(make_worker, tmp_path, _BoxIdFake("box-B"))
    with pytest.raises(ProtocolError, match="box-affinity check failed"):
        await worker._verify_home_affinity()


@pytest.mark.asyncio
async def test_affinity_old_backend_warns_only(make_worker, tmp_path, caplog):
    (tmp_path / ".box-id").write_text("box-A")
    worker = _affinity_worker(make_worker, tmp_path, _BoxIdFake(None))
    with caplog.at_level(logging.WARNING):
        await worker._verify_home_affinity()
    assert any("predates" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_affinity_missing_sentinel_warns_only(make_worker, tmp_path, caplog):
    worker = _affinity_worker(make_worker, tmp_path, _BoxIdFake("box-A"))
    with caplog.at_level(logging.WARNING):
        await worker._verify_home_affinity()
    assert any("no readable" in r.message for r in caplog.records)
