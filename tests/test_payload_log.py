"""Unit tests for PayloadLogger.

PayloadLogger captures task envelopes to JSONL files inside a worker's
shared volume mount. These tests use ``tmp_path`` so they don't need a
real /app/shared mount; the production root is wired by Worker.__init__.
"""
from __future__ import annotations

import re
from pathlib import Path

from task_worker_api.payload_log import PayloadLogger


def test_disabled_logger_does_not_create_root(tmp_path: Path):
    """Constructing with enabled=False must not touch the filesystem."""
    root = tmp_path / "_worker_payloads" / "test-worker"
    PayloadLogger(root=root, worker_id="test-worker", enabled=False)
    assert not root.exists()


def test_enabled_init_creates_root_directory(tmp_path: Path):
    root = tmp_path / "_worker_payloads" / "test-worker"
    PayloadLogger(root=root, worker_id="test-worker", enabled=True)
    assert root.is_dir()


def test_boot_id_is_8_hex_chars_by_default(tmp_path: Path):
    logger = PayloadLogger(
        root=tmp_path / "_worker_payloads" / "test-worker",
        worker_id="test-worker",
        enabled=True,
    )
    assert re.fullmatch(r"[0-9a-f]{8}", logger.boot_id) is not None


def test_boot_id_can_be_injected_for_tests(tmp_path: Path):
    logger = PayloadLogger(
        root=tmp_path / "_worker_payloads" / "test-worker",
        worker_id="test-worker",
        enabled=True,
        _boot_id="deadbeef",
    )
    assert logger.boot_id == "deadbeef"


# ----- sanitize_worker_id ----------------------------------------------------

from task_worker_api.payload_log import sanitize_worker_id


def test_sanitize_replaces_unsafe_chars():
    assert sanitize_worker_id("blender-worker-1") == "blender-worker-1"
    assert sanitize_worker_id("worker/1") == "worker_1"
    assert sanitize_worker_id("worker\\1") == "worker_1"
    assert sanitize_worker_id("worker:1") == "worker_1"
    assert sanitize_worker_id("../etc") == ".._etc"  # `.` is allowed; `/` becomes `_`


def test_sanitize_rejects_dot_only_names():
    assert sanitize_worker_id(".") == "._x"
    assert sanitize_worker_id("..") == ".._x"
    assert sanitize_worker_id("") == "_x"


def test_sanitize_rejects_windows_reserved_names():
    assert sanitize_worker_id("CON") == "CON_x"
    assert sanitize_worker_id("nul") == "nul_x"
    assert sanitize_worker_id("COM3") == "COM3_x"
    assert sanitize_worker_id("LPT9") == "LPT9_x"
    # extension after reserved name is still reserved on Windows
    assert sanitize_worker_id("CON.log") == "CON.log_x"


def test_sanitize_output_never_contains_separators():
    assert "/" not in sanitize_worker_id("worker/1")
    assert "\\" not in sanitize_worker_id("worker\\1")


# ----- record() typed stream -------------------------------------------------

import json
from datetime import datetime, timezone
from typing import Optional

from task_worker_api.context import ClaimedTask
from task_worker_api.enums import TaskStatus, TaskType


def _make_task(task_id: int = 1, params: Optional[dict] = None) -> ClaimedTask:
    return ClaimedTask(
        id=task_id,
        task_type=TaskType.DETECT_CUT_PLANES,
        case_id=42,
        item_key="case_42_scene_1",
        status=TaskStatus.IN_PROGRESS,
        params=params if params is not None else {"input_path": "/tmp/x.stl", "max_results": 3},
        worker_id="test-worker",
    )


def _fixed_now() -> datetime:
    return datetime(2026, 4, 26, 14, 23, 11, 234567, tzinfo=timezone.utc)


def test_record_writes_one_typed_line(tmp_path: Path):
    root = tmp_path / "_worker_payloads" / "test-worker"
    logger = PayloadLogger(
        root=root,
        worker_id="test-worker",
        enabled=True,
        _boot_id="deadbeef",
        _now=_fixed_now,
        _pid=lambda: 12345,
    )

    logger.record(_make_task())

    files = list(root.glob("payloads-*.jsonl"))
    assert len(files) == 1
    assert files[0].name == "payloads-2026-04-26-pid12345-deadbeef.jsonl"

    lines = files[0].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1

    entry = json.loads(lines[0])
    assert entry["stream"] == "typed"
    assert entry["task_id"] == 1
    assert entry["task_type"] == "detect_cut_planes"
    assert entry["case_id"] == 42
    assert entry["item_key"] == "case_42_scene_1"
    assert entry["params"] == {"input_path": "/tmp/x.stl", "max_results": 3}
    assert entry["worker_id"] == "test-worker"
    assert entry["process_id"] == 12345
    assert entry["boot_id"] == "deadbeef"
    assert entry["captured_at"] == "2026-04-26T14:23:11.234567+00:00"


def test_record_rotates_on_utc_date_change(tmp_path: Path):
    """Date check fires at the start of every record() call."""
    root = tmp_path / "_worker_payloads" / "test-worker"
    times = iter([
        datetime(2026, 4, 26, 23, 59, 59, tzinfo=timezone.utc),
        datetime(2026, 4, 27, 0, 0, 30, tzinfo=timezone.utc),
    ])
    logger = PayloadLogger(
        root=root, worker_id="test-worker", enabled=True,
        _boot_id="deadbeef", _pid=lambda: 1, _now=lambda: next(times),
    )
    logger.record(_make_task(task_id=1))
    logger.record(_make_task(task_id=2))

    files = sorted(p.name for p in root.glob("payloads-*.jsonl"))
    assert files == [
        "payloads-2026-04-26-pid1-deadbeef.jsonl",
        "payloads-2026-04-27-pid1-deadbeef.jsonl",
    ]


def test_record_rotates_after_idle_across_multiple_days(tmp_path: Path):
    """A single record() after several idle days lands in the new date's file."""
    root = tmp_path / "_worker_payloads" / "test-worker"
    times = iter([
        datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 5, 3, 12, 0, 0, tzinfo=timezone.utc),
    ])
    logger = PayloadLogger(
        root=root, worker_id="test-worker", enabled=True,
        _boot_id="deadbeef", _pid=lambda: 1, _now=lambda: next(times),
    )
    logger.record(_make_task(task_id=1))
    logger.record(_make_task(task_id=2))

    files = sorted(p.name for p in root.glob("payloads-*.jsonl"))
    assert "payloads-2026-04-26-pid1-deadbeef.jsonl" in files
    assert "payloads-2026-05-03-pid1-deadbeef.jsonl" in files


def test_record_handles_non_json_serializable_params(tmp_path: Path):
    root = tmp_path / "_worker_payloads" / "test-worker"
    logger = PayloadLogger(
        root=root, worker_id="test-worker", enabled=True,
        _boot_id="deadbeef", _pid=lambda: 1, _now=_fixed_now,
    )
    bad_params = {
        "input_path": Path("/tmp/scene.stl"),
        "started_at": datetime(2026, 4, 26, tzinfo=timezone.utc),
    }
    logger.record(_make_task(params=bad_params))

    line = (root / "payloads-2026-04-26-pid1-deadbeef.jsonl").read_text(encoding="utf-8")
    entry = json.loads(line)
    # default=str converts both to strings — no crash, no skipped record
    assert isinstance(entry["params"]["input_path"], str)
    assert "scene.stl" in entry["params"]["input_path"]
    assert "2026-04-26" in entry["params"]["started_at"]


def test_record_truncates_oversized_params(tmp_path: Path):
    root = tmp_path / "_worker_payloads" / "test-worker"
    logger = PayloadLogger(
        root=root, worker_id="test-worker", enabled=True,
        _boot_id="deadbeef", _pid=lambda: 1, _now=_fixed_now,
    )
    huge = {"blob": "x" * (300 * 1024)}  # > 224KB cap
    logger.record(_make_task(params=huge))

    entry = json.loads(
        (root / "payloads-2026-04-26-pid1-deadbeef.jsonl").read_text(encoding="utf-8")
    )
    assert entry["params"]["_truncated"] is True
    assert entry["params"]["_original_size_bytes"] > 224 * 1024
    # task metadata still present after truncation
    assert entry["task_id"] == 1
    assert entry["task_type"] == "detect_cut_planes"


def test_final_record_size_cap_applies_when_non_param_fields_huge(tmp_path: Path):
    root = tmp_path / "_worker_payloads" / "test-worker"
    logger = PayloadLogger(
        root=root, worker_id="test-worker", enabled=True,
        _boot_id="deadbeef", _pid=lambda: 1, _now=_fixed_now,
    )
    # Pathological: small params, but the item_key itself is huge.
    task = ClaimedTask(
        id=99,
        task_type=TaskType.DETECT_CUT_PLANES,
        case_id=1,
        item_key="X" * (300 * 1024),
        status=TaskStatus.IN_PROGRESS,
        params={"k": "v"},
        worker_id="test-worker",
    )
    logger.record(task)

    entry = json.loads(
        (root / "payloads-2026-04-26-pid1-deadbeef.jsonl").read_text(encoding="utf-8")
    )
    assert entry["_record_truncated"] is True
    assert entry["task_id"] == 99
    assert "captured_at" in entry


def test_record_never_raises_on_open_failure(tmp_path: Path, caplog, monkeypatch):
    """A failure in open() must not propagate; one WARNING logged. Subsequent
    calls keep retrying — degraded mode suppresses warnings, not retries."""
    import builtins
    real_open = builtins.open

    calls = {"n": 0}

    def fake_open(path, *args, **kwargs):
        if "_worker_payloads" in str(path) and "payloads-" in str(path):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("disk full")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)

    root = tmp_path / "_worker_payloads" / "test-worker"
    logger = PayloadLogger(
        root=root, worker_id="test-worker", enabled=True,
        _boot_id="deadbeef", _pid=lambda: 1, _now=_fixed_now,
    )
    with caplog.at_level("WARNING"):
        logger.record(_make_task(task_id=1))  # first call: open fails
        logger.record(_make_task(task_id=2))  # second call: open succeeds

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "I/O failure" in warnings[0].message

    # Second record landed on disk — degraded mode kept retrying.
    files = list(root.glob("payloads-*.jsonl"))
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8").strip()


def test_repeat_failures_suppress_warning(tmp_path: Path, caplog, monkeypatch):
    """After the first WARNING, subsequent failures stay silent."""
    import builtins
    real_open = builtins.open

    def always_fail_open(path, *args, **kwargs):
        if "_worker_payloads" in str(path) and "payloads-" in str(path):
            raise OSError("permanent")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", always_fail_open)

    root = tmp_path / "_worker_payloads" / "test-worker"
    logger = PayloadLogger(
        root=root, worker_id="test-worker", enabled=True,
        _boot_id="deadbeef", _pid=lambda: 1, _now=_fixed_now,
    )
    with caplog.at_level("WARNING"):
        for i in range(5):
            logger.record(_make_task(task_id=i))

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1


def test_record_raw_writes_to_raw_envelopes_stream(tmp_path: Path):
    root = tmp_path / "_worker_payloads" / "test-worker"
    logger = PayloadLogger(
        root=root, worker_id="test-worker", enabled=True,
        _boot_id="deadbeef", _pid=lambda: 1, _now=_fixed_now,
    )
    raw = {"id": 7, "task_type": 999, "params": {"k": "v"}}
    logger.record_raw(raw, error_type="ValueError", error="999 is not a valid TaskType")

    files = list(root.glob("raw_envelopes-*.jsonl"))
    assert len(files) == 1
    entry = json.loads(files[0].read_text(encoding="utf-8"))
    assert entry["stream"] == "raw"
    assert entry["raw"] == raw
    assert entry["error_type"] == "ValueError"
    assert entry["error"] == "999 is not a valid TaskType"
    assert entry["worker_id"] == "test-worker"


def test_record_raw_handles_non_dict_raw(tmp_path: Path):
    """raw can be None, a list, or a parse-failure string."""
    root = tmp_path / "_worker_payloads" / "test-worker"
    logger = PayloadLogger(
        root=root, worker_id="test-worker", enabled=True,
        _boot_id="deadbeef", _pid=lambda: 1, _now=_fixed_now,
    )
    logger.record_raw(None, error_type="TypeError", error="expected dict")
    logger.record_raw([1, 2, 3], error_type="TypeError", error="got list")
    logger.record_raw("<html>500</html>", error_type="JSONDecodeError", error="bad")

    lines = (
        root / "raw_envelopes-2026-04-26-pid1-deadbeef.jsonl"
    ).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    parsed = [json.loads(line) for line in lines]
    assert parsed[0]["raw"] is None
    assert parsed[1]["raw"] == [1, 2, 3]
    assert parsed[2]["raw"] == "<html>500</html>"


# ----- cleanup_old_files -----------------------------------------------------

import os
import time


def _create_old_file(path: Path, age_days: float) -> None:
    path.write_text("{}\n", encoding="utf-8")
    age_seconds = age_days * 86400
    mtime = time.time() - age_seconds
    os.utime(path, (mtime, mtime))


def test_cleanup_removes_files_at_or_past_retention(tmp_path: Path):
    root = tmp_path / "_worker_payloads" / "test-worker"
    root.mkdir(parents=True)
    seven_d = root / "payloads-2026-04-19-pid1-aaaa.jsonl"
    fourteen_d = root / "payloads-2026-04-12-pid1-bbbb.jsonl"
    thirty_d = root / "payloads-2026-03-27-pid1-cccc.jsonl"
    raw_thirty_d = root / "raw_envelopes-2026-03-27-pid1-dddd.jsonl"
    _create_old_file(seven_d, 7)
    _create_old_file(fourteen_d, 14.5)  # past the inclusive boundary
    _create_old_file(thirty_d, 30)
    _create_old_file(raw_thirty_d, 30)

    logger = PayloadLogger(
        root=root, worker_id="test-worker", retention_days=14, enabled=True,
        _boot_id="deadbeef", _pid=lambda: 1,
    )
    logger.cleanup_old_files()

    assert seven_d.exists()
    assert not fourteen_d.exists()
    assert not thirty_d.exists()
    assert not raw_thirty_d.exists()  # both streams cleaned


def test_cleanup_tolerates_permission_error(tmp_path: Path, monkeypatch):
    root = tmp_path / "_worker_payloads" / "test-worker"
    root.mkdir(parents=True)
    a = root / "payloads-2026-03-27-pid1-aaaa.jsonl"
    b = root / "payloads-2026-03-27-pid1-bbbb.jsonl"
    _create_old_file(a, 30)
    _create_old_file(b, 30)

    real_unlink = Path.unlink
    def flaky_unlink(self, *args, **kwargs):
        if self.name.endswith("aaaa.jsonl"):
            raise PermissionError("file held open by another process")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)

    logger = PayloadLogger(
        root=root, worker_id="test-worker", retention_days=14, enabled=True,
        _boot_id="deadbeef", _pid=lambda: 1,
    )
    logger.cleanup_old_files()  # must not raise

    assert a.exists()       # PermissionError-raising one survived
    assert not b.exists()   # the other was still removed


def test_cleanup_runs_even_when_disabled(tmp_path: Path):
    """A deployment that flips enabled=false shouldn't accumulate forever."""
    root = tmp_path / "_worker_payloads" / "test-worker"
    root.mkdir(parents=True)
    expired = root / "payloads-2026-03-27-pid1-aaaa.jsonl"
    _create_old_file(expired, 30)

    logger = PayloadLogger(
        root=root, worker_id="test-worker", retention_days=14, enabled=False,
    )
    logger.cleanup_old_files()
    assert not expired.exists()
