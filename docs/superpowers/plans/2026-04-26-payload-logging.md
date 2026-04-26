# Worker Payload Logging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture every claimed task's full envelope to a per-worker JSONL file inside the worker container so an operator can reproduce a worker issue or replay real producer traffic into tests.

**Architecture:** A new `PayloadLogger` class owned by `Worker` writes two append-only streams (`payloads-*.jsonl` and `raw_envelopes-*.jsonl`) under `/app/shared/_worker_payloads/{worker_id}/` with daily rotation, PID + boot-id uniqueness for scaled replicas, mtime-based retention (default 14 days, with periodic + rollover + startup cleanup), and a never-raises failure contract. `BackendClient.claim_next` writes the raw-envelope sidecar when `ClaimedTask.from_dict()` or `response.json()` fails (protocol drift). Default-on; opt-out via `WORKER_PAYLOAD_LOG_ENABLED=false`.

**Tech Stack:** Python 3.10+, pytest + pytest-asyncio, httpx (existing), `httpx.MockTransport` for protocol-level tests (no new dep), pure stdlib for the logger itself.

**Spec:** `docs/superpowers/specs/2026-04-26-payload-logging-design.md` (commit `bd35a50`)

---

## Phase 1 — `PayloadLogger` module (TDD)

### Task 1: Skeleton + disabled-mode short-circuit

**Files:**
- Create: `src/task_worker_api/payload_log.py`
- Create: `tests/test_payload_log.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_payload_log.py
"""Unit tests for PayloadLogger.

PayloadLogger captures task envelopes to JSONL files inside a worker's
shared volume mount. These tests use ``tmp_path`` so they don't need a
real /app/shared mount; the production root is wired by Worker.__init__.
"""
from __future__ import annotations

from pathlib import Path

from task_worker_api.payload_log import PayloadLogger


def test_disabled_logger_does_not_create_root(tmp_path: Path):
    """Constructing with enabled=False must not touch the filesystem."""
    root = tmp_path / "_worker_payloads" / "test-worker"
    PayloadLogger(root=root, worker_id="test-worker", enabled=False)
    assert not root.exists()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd p:/Project/task-worker-api
pytest tests/test_payload_log.py::test_disabled_logger_does_not_create_root -v
```
Expected: `ImportError: cannot import name 'PayloadLogger' from 'task_worker_api.payload_log'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/task_worker_api/payload_log.py
"""Per-worker JSONL capture of every claimed task envelope.

See docs/superpowers/specs/2026-04-26-payload-logging-design.md for the
full design rationale (two streams, per-process files for scaled replicas,
mtime-based retention, never-raises contract).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional


class PayloadLogger:
    """Append claimed-task envelopes to per-worker JSONL files.

    Owned by Worker; never instantiated directly by SDK consumers. The
    failure contract is broad: every public method must return without
    raising, even on disk-full / permission / serialization errors.
    """

    def __init__(
        self,
        *,
        root: Path,
        worker_id: str,
        retention_days: int = 14,
        enabled: bool = True,
    ) -> None:
        self.root = root
        self.worker_id = worker_id
        self.retention_days = retention_days
        self.enabled = enabled
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_payload_log.py::test_disabled_logger_does_not_create_root -v
```
Expected: `PASSED`

- [ ] **Step 5: Commit**

```bash
git add src/task_worker_api/payload_log.py tests/test_payload_log.py
git commit -m "feat(payload-log): skeleton PayloadLogger with disabled-mode short-circuit"
```

---

### Task 2: Enabled init creates root + generates 8-char boot_id

**Files:**
- Modify: `src/task_worker_api/payload_log.py`
- Modify: `tests/test_payload_log.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_payload_log.py`:

```python
import re


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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_payload_log.py -v -k "init_creates_root or boot_id"
```
Expected: 3 failures (`AttributeError: 'PayloadLogger' object has no attribute 'boot_id'`)

- [ ] **Step 3: Update implementation**

Replace the body of `payload_log.py`:

```python
"""Per-worker JSONL capture of every claimed task envelope.

See docs/superpowers/specs/2026-04-26-payload-logging-design.md for the
full design rationale.
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Optional


class PayloadLogger:
    """Append claimed-task envelopes to per-worker JSONL files."""

    def __init__(
        self,
        *,
        root: Path,
        worker_id: str,
        retention_days: int = 14,
        enabled: bool = True,
        _boot_id: Optional[str] = None,
    ) -> None:
        self.root = root
        self.worker_id = worker_id
        self.retention_days = retention_days
        self.enabled = enabled
        self.boot_id = _boot_id or uuid.uuid4().hex[:8]
        if self.enabled:
            self.root.mkdir(parents=True, exist_ok=True)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_payload_log.py -v
```
Expected: 4 passing

- [ ] **Step 5: Commit**

```bash
git add src/task_worker_api/payload_log.py tests/test_payload_log.py
git commit -m "feat(payload-log): enabled init creates root + 8-char boot_id"
```

---

### Task 3: `worker_id` sanitization for path safety

**Files:**
- Modify: `src/task_worker_api/payload_log.py`
- Modify: `tests/test_payload_log.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_payload_log.py`:

```python
from task_worker_api.payload_log import sanitize_worker_id


def test_sanitize_replaces_unsafe_chars():
    assert sanitize_worker_id("blender-worker-1") == "blender-worker-1"
    assert sanitize_worker_id("worker/1") == "worker_1"
    assert sanitize_worker_id("worker\\1") == "worker_1"
    assert sanitize_worker_id("worker:1") == "worker_1"
    assert sanitize_worker_id("../etc") == "_._etc"


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


def test_init_sanitizes_worker_id_into_path(tmp_path: Path):
    """Constructor must use the sanitized name when creating the root."""
    # Caller passes a clean root; the path-segment safety happens at the
    # Worker layer when it builds root from worker_id. Here we assert the
    # exported helper produces safe values.
    assert "/" not in sanitize_worker_id("worker/1")
    assert "\\" not in sanitize_worker_id("worker\\1")
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_payload_log.py -v -k "sanitize"
```
Expected: `ImportError: cannot import name 'sanitize_worker_id'`

- [ ] **Step 3: Update implementation**

Add to `payload_log.py` (above the class):

```python
import re

_WINDOWS_RESERVED = (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def sanitize_worker_id(worker_id: str) -> str:
    """Make a worker_id safe to use as a path segment on Linux and Windows.

    Replaces characters outside ``[A-Za-z0-9._-]`` with ``_`` (covers
    forward/back slashes, colons, spaces, etc.). Then appends ``_x`` if
    the result is empty, dot-only, or matches a Windows reserved device
    name (CON, PRN, AUX, NUL, COM1-9, LPT1-9, with or without an
    extension — Windows treats e.g. ``CON.log`` as the device too).

    The output is purely a path segment — no separators ever appear.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", worker_id)
    base_for_check = cleaned.split(".", 1)[0].upper()
    if (
        not cleaned
        or cleaned in {".", ".."}
        or base_for_check in _WINDOWS_RESERVED
    ):
        cleaned = cleaned + "_x"
    return cleaned
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_payload_log.py -v
```
Expected: all passing

- [ ] **Step 5: Commit**

```bash
git add src/task_worker_api/payload_log.py tests/test_payload_log.py
git commit -m "feat(payload-log): sanitize_worker_id for cross-platform path safety"
```

---

### Task 4: `record()` writes one typed-stream JSON line

**Files:**
- Modify: `src/task_worker_api/payload_log.py`
- Modify: `tests/test_payload_log.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_payload_log.py`:

```python
import json
from datetime import datetime, timezone

from task_worker_api.context import ClaimedTask
from task_worker_api.enums import TaskStatus, TaskType


def _make_task(task_id: int = 1, params: Optional[dict] = None) -> ClaimedTask:
    return ClaimedTask(
        id=task_id,
        task_type=TaskType.DETECT_CUT_PLANES,
        case_id=42,
        item_key="case_42_scene_1",
        status=TaskStatus.RUNNING,
        params=params or {"input_path": "/tmp/x.stl", "max_results": 3},
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
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_payload_log.py::test_record_writes_one_typed_line -v
```
Expected: failure (logger has no `record` method, and constructor doesn't accept `_now` / `_pid`)

- [ ] **Step 3: Update implementation**

Replace `payload_log.py` with this expanded version:

```python
"""Per-worker JSONL capture of every claimed task envelope."""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, TextIO

from .context import ClaimedTask

log = logging.getLogger(__name__)

_WINDOWS_RESERVED = (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def sanitize_worker_id(worker_id: str) -> str:
    """Make a worker_id safe to use as a path segment on Linux and Windows."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", worker_id)
    base_for_check = cleaned.split(".", 1)[0].upper()
    if (
        not cleaned
        or cleaned in {".", ".."}
        or base_for_check in _WINDOWS_RESERVED
    ):
        cleaned = cleaned + "_x"
    return cleaned


class PayloadLogger:
    """Append claimed-task envelopes to per-worker JSONL files.

    Owned by Worker; never instantiated directly by SDK consumers. The
    failure contract is broad: every public method must return without
    raising, even on disk-full / permission / serialization errors.
    """

    def __init__(
        self,
        *,
        root: Path,
        worker_id: str,
        retention_days: int = 14,
        enabled: bool = True,
        _now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        _pid: Callable[[], int] = os.getpid,
        _boot_id: Optional[str] = None,
    ) -> None:
        self.root = root
        self.worker_id = worker_id
        self.retention_days = retention_days
        self.enabled = enabled
        self._now = _now
        self._pid = _pid
        self.boot_id = _boot_id or uuid.uuid4().hex[:8]
        # per-stream {date, handle} state
        self._handles: dict[str, tuple[str, TextIO]] = {}
        self._warned_once = False
        if self.enabled:
            self.root.mkdir(parents=True, exist_ok=True)

    # ----- public API -------------------------------------------------

    def record(self, task: ClaimedTask) -> None:
        """Append one typed-stream JSON line for this task. Never raises."""
        if not self.enabled:
            return
        try:
            record = {
                "captured_at": self._now().isoformat(),
                "stream": "typed",
                "task_id": task.id,
                "task_type": task.task_type.value,
                "case_id": task.case_id,
                "item_key": task.item_key,
                "status": int(task.status),
                "params": task.params,
                "worker_id": task.worker_id or self.worker_id,
                "process_id": self._pid(),
                "boot_id": self.boot_id,
            }
            self._write_line("payloads", record)
        except Exception as exc:  # noqa: BLE001 — never-raises contract
            self._warn_once(exc)

    # ----- internals --------------------------------------------------

    def _file_path(self, stream: str, date_str: str) -> Path:
        return self.root / (
            f"{stream}-{date_str}-pid{self._pid()}-{self.boot_id}.jsonl"
        )

    def _ensure_handle(self, stream: str) -> TextIO:
        date_str = self._now().date().isoformat()
        cached = self._handles.get(stream)
        if cached is None or cached[0] != date_str:
            if cached is not None:
                try:
                    cached[1].close()
                except Exception:  # noqa: BLE001
                    pass
            handle = open(self._file_path(stream, date_str), "a", encoding="utf-8")
            self._handles[stream] = (date_str, handle)
            return handle
        return cached[1]

    def _write_line(self, stream: str, record: dict) -> None:
        line = json.dumps(record, default=str)
        handle = self._ensure_handle(stream)
        handle.write(line + "\n")
        handle.flush()

    def _warn_once(self, exc: BaseException) -> None:
        if self._warned_once:
            return
        self._warned_once = True
        log.warning(
            "payload_log: I/O failure (further failures suppressed): %s: %s",
            type(exc).__name__, exc,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_payload_log.py -v
```
Expected: all passing

- [ ] **Step 5: Commit**

```bash
git add src/task_worker_api/payload_log.py tests/test_payload_log.py
git commit -m "feat(payload-log): record() writes typed-stream JSONL with stable schema"
```

---

### Task 5: `record()` rotates file on UTC date change

**Files:**
- Modify: `tests/test_payload_log.py`

The implementation in Task 4 already handles rotation via `_ensure_handle`; this task validates that with a test driving `_now` across midnight.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_payload_log.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they pass**

```bash
pytest tests/test_payload_log.py -v -k "rotate"
```
Expected: passing (Task 4's `_ensure_handle` already implements rotation)

- [ ] **Step 3: Commit**

```bash
git add tests/test_payload_log.py
git commit -m "test(payload-log): validate UTC date rollover and idle-across-days rotation"
```

---

### Task 6: Non-JSON-serializable values use `default=str`

**Files:**
- Modify: `tests/test_payload_log.py`

Implementation already uses `default=str`; this task adds a regression test.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test**

```bash
pytest tests/test_payload_log.py::test_record_handles_non_json_serializable_params -v
```
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_payload_log.py
git commit -m "test(payload-log): non-serializable params (Path, datetime) survive default=str"
```

---

### Task 7: Two-stage size cap (256KB final, 224KB pre-serialization)

**Files:**
- Modify: `src/task_worker_api/payload_log.py`
- Modify: `tests/test_payload_log.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_payload_log.py`:

```python
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
        status=TaskStatus.RUNNING,
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_payload_log.py -v -k "truncate or final_record"
```
Expected: 2 failures (no truncation logic yet)

- [ ] **Step 3: Update implementation**

Replace `_write_line` and add helpers in `payload_log.py`:

```python
PAYLOAD_FIELD_CAP_BYTES = 224 * 1024  # leaves headroom for envelope fields
RECORD_CAP_BYTES = 256 * 1024


# inside class PayloadLogger:

    def _maybe_truncate_field(self, value: Any) -> tuple[Any, Optional[int]]:
        """Return (value-or-marker, original_size_bytes_if_truncated_else_None).

        Serializes ``value`` exactly once. If the JSON form exceeds the cap,
        returns a small marker dict and the original byte count; otherwise
        returns the value unchanged. The caller is responsible for using the
        returned value when constructing the final record.
        """
        try:
            serialized = json.dumps(value, default=str)
        except Exception:  # noqa: BLE001 — caller's outer try/except will warn
            raise
        size = len(serialized.encode("utf-8"))
        if size > PAYLOAD_FIELD_CAP_BYTES:
            return {"_truncated": True, "_original_size_bytes": size}, size
        return value, None

    def _serialize_record(self, record: dict) -> str:
        line = json.dumps(record, default=str)
        if len(line.encode("utf-8")) <= RECORD_CAP_BYTES:
            return line
        # Stage 2: even after payload truncation, the full record is too big
        # (e.g., pathological item_key/error/worker_id). Replace the body but
        # keep enough metadata to know what happened.
        original_size = len(line.encode("utf-8"))
        self._warn_truncated_once(record.get("task_id"), "post-construction", original_size)
        replacement = {
            "_record_truncated": True,
            "_original_size_bytes": original_size,
            "task_id": record.get("task_id"),
            "stream": record.get("stream"),
            "captured_at": record.get("captured_at"),
        }
        return json.dumps(replacement, default=str)

    def _warn_truncated_once(self, task_id: Any, stage: str, size: int) -> None:
        if getattr(self, "_truncate_warned", False):
            return
        self._truncate_warned = True
        log.warning(
            "payload_log: truncated record (task_id=%s, stage=%s, size=%d bytes); "
            "further truncations suppressed",
            task_id, stage, size,
        )

    def _write_line(self, stream: str, record: dict) -> None:
        line = self._serialize_record(record)
        handle = self._ensure_handle(stream)
        handle.write(line + "\n")
        handle.flush()
```

Update `record()` to use the field-level truncation:

```python
    def record(self, task: ClaimedTask) -> None:
        if not self.enabled:
            return
        try:
            safe_params, truncated_size = self._maybe_truncate_field(task.params)
            if truncated_size:
                self._warn_truncated_once(task.id, "pre-serialization", truncated_size)
            record = {
                "captured_at": self._now().isoformat(),
                "stream": "typed",
                "task_id": task.id,
                "task_type": task.task_type.value,
                "case_id": task.case_id,
                "item_key": task.item_key,
                "status": int(task.status),
                "params": safe_params,
                "worker_id": task.worker_id or self.worker_id,
                "process_id": self._pid(),
                "boot_id": self.boot_id,
            }
            self._write_line("typed", record)  # NB: stream label, not file stem
        except Exception as exc:  # noqa: BLE001
            self._warn_once(exc)
```

Wait — the file stem is `payloads-...` but the stream label inside the record was already `"typed"`. Keep `_write_line(stream="payloads", ...)` aligned with the file stem so `_ensure_handle("payloads")` opens `payloads-*.jsonl`. The `record["stream"]` field is purely a JSON label.

Fix: change the call back to `self._write_line("payloads", record)`. Update tests if needed.

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_payload_log.py -v
```
Expected: all passing

- [ ] **Step 5: Commit**

```bash
git add src/task_worker_api/payload_log.py tests/test_payload_log.py
git commit -m "feat(payload-log): two-stage size cap (224KB field, 256KB final record)"
```

---

### Task 8: Never-raises contract + degraded-mode keeps retrying

**Files:**
- Modify: `tests/test_payload_log.py`

Implementation already handles this; this task adds explicit tests.

- [ ] **Step 1: Write the failing tests**

```python
def test_record_never_raises_on_open_failure(tmp_path: Path, caplog, monkeypatch):
    """A failure in open() must not propagate; one WARNING logged."""
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

    # First call's failure produced one WARNING; never raised.
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "I/O failure" in warnings[0].message
    # Second call succeeded — degraded mode does not disable retries.
    files = list(root.glob("payloads-*.jsonl"))
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8").strip()  # has content


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
```

- [ ] **Step 2: Run tests**

```bash
pytest tests/test_payload_log.py -v -k "never_raises or repeat_failures"
```
Expected: PASS (Task 4 already implements this via `_warn_once`)

- [ ] **Step 3: Commit**

```bash
git add tests/test_payload_log.py
git commit -m "test(payload-log): never-raises + degraded-mode retries with one warning"
```

---

### Task 9: `record_raw()` for the raw_envelopes stream

**Files:**
- Modify: `src/task_worker_api/payload_log.py`
- Modify: `tests/test_payload_log.py`

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_payload_log.py -v -k "record_raw"
```
Expected: failure (no `record_raw` method)

- [ ] **Step 3: Add the method**

Add to `PayloadLogger` in `payload_log.py`:

```python
    def record_raw(
        self,
        raw: Any,
        *,
        error_type: str,
        error: str,
    ) -> None:
        """Append one raw-envelope JSON line. Never raises. No-op when disabled.

        Called from BackendClient.claim_next when ClaimedTask.from_dict() raises
        OR when the response itself fails to parse as JSON. ``raw`` may be any
        JSON-coercible value (dict, list, None) or unparseable text.
        """
        if not self.enabled:
            return
        try:
            safe_raw, truncated_size = self._maybe_truncate_field(raw)
            if truncated_size:
                self._warn_truncated_once(None, "pre-serialization", truncated_size)
            record = {
                "captured_at": self._now().isoformat(),
                "stream": "raw",
                "raw": safe_raw,
                "error_type": error_type,
                "error": error,
                "worker_id": self.worker_id,
                "process_id": self._pid(),
                "boot_id": self.boot_id,
            }
            self._write_line("raw_envelopes", record)
        except Exception as exc:  # noqa: BLE001
            self._warn_once(exc)
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_payload_log.py -v
```
Expected: all passing

- [ ] **Step 5: Commit**

```bash
git add src/task_worker_api/payload_log.py tests/test_payload_log.py
git commit -m "feat(payload-log): record_raw() for protocol-drift envelope capture"
```

---

### Task 10: `cleanup_old_files()` mtime-based retention

**Files:**
- Modify: `src/task_worker_api/payload_log.py`
- Modify: `tests/test_payload_log.py`

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_payload_log.py::test_cleanup_removes_files_at_or_past_retention -v
```
Expected: failure (no `cleanup_old_files` method)

- [ ] **Step 3: Add the method**

Add to `PayloadLogger`:

```python
    def cleanup_old_files(self) -> None:
        """Delete files past retention. Runs even when disabled. Never raises."""
        try:
            if not self.root.exists():
                return
            cutoff_seconds = self.retention_days * 86400
            now_ts = time.time()
            for entry in self.root.iterdir():
                if not entry.is_file():
                    continue
                if not (
                    entry.name.startswith("payloads-")
                    or entry.name.startswith("raw_envelopes-")
                ):
                    continue
                if not entry.name.endswith(".jsonl"):
                    continue
                try:
                    age = now_ts - entry.stat().st_mtime
                except OSError:
                    continue
                if age >= cutoff_seconds:
                    try:
                        entry.unlink()
                    except (FileNotFoundError, PermissionError, OSError):
                        # Race with another replica or a Windows handle still
                        # open elsewhere. Skip and continue.
                        continue
        except Exception as exc:  # noqa: BLE001
            self._warn_once(exc)
```

Add `import time` at the top of the file.

- [ ] **Step 4: Run test**

```bash
pytest tests/test_payload_log.py::test_cleanup_removes_files_at_or_past_retention -v
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/task_worker_api/payload_log.py tests/test_payload_log.py
git commit -m "feat(payload-log): cleanup_old_files with mtime retention boundary"
```

---

### Task 11: Cleanup tolerates `PermissionError` and runs while disabled

**Files:**
- Modify: `tests/test_payload_log.py`

Implementation in Task 10 already covers both behaviors. Add tests.

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run tests**

```bash
pytest tests/test_payload_log.py -v -k "cleanup"
```
Expected: all passing

- [ ] **Step 3: Commit**

```bash
git add tests/test_payload_log.py
git commit -m "test(payload-log): cleanup tolerates PermissionError and runs while disabled"
```

---

### Task 12: `close()` is idempotent

**Files:**
- Modify: `src/task_worker_api/payload_log.py`
- Modify: `tests/test_payload_log.py`

- [ ] **Step 1: Write the failing test**

```python
def test_close_is_idempotent_and_flushes_handles(tmp_path: Path):
    root = tmp_path / "_worker_payloads" / "test-worker"
    logger = PayloadLogger(
        root=root, worker_id="test-worker", enabled=True,
        _boot_id="deadbeef", _pid=lambda: 1, _now=_fixed_now,
    )
    logger.record(_make_task())
    logger.close()
    logger.close()  # second call must not raise

    # File contents are durable after close
    line = (
        root / "payloads-2026-04-26-pid1-deadbeef.jsonl"
    ).read_text(encoding="utf-8")
    assert json.loads(line)["task_id"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_payload_log.py::test_close_is_idempotent_and_flushes_handles -v
```
Expected: failure (no `close` method)

- [ ] **Step 3: Add the method**

```python
    def close(self) -> None:
        """Flush + close any open handles. Idempotent. Never raises."""
        try:
            for date_str, handle in list(self._handles.values()):
                try:
                    handle.flush()
                    handle.close()
                except Exception:  # noqa: BLE001
                    continue
            self._handles.clear()
        except Exception as exc:  # noqa: BLE001
            self._warn_once(exc)
```

- [ ] **Step 4: Run test**

```bash
pytest tests/test_payload_log.py::test_close_is_idempotent_and_flushes_handles -v
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/task_worker_api/payload_log.py tests/test_payload_log.py
git commit -m "feat(payload-log): close() flushes and is idempotent"
```

---

### Task 13: `__init__` self-disables on mkdir failure (never-raises contract)

**Files:**
- Modify: `src/task_worker_api/payload_log.py`
- Modify: `tests/test_payload_log.py`

- [ ] **Step 1: Write the failing test**

```python
def test_init_self_disables_on_mkdir_permission_error(tmp_path: Path, caplog, monkeypatch):
    """A PermissionError in mkdir must not crash Worker construction."""
    real_mkdir = Path.mkdir
    def fail_mkdir(self, *args, **kwargs):
        if "_worker_payloads" in str(self):
            raise PermissionError("read-only volume")
        return real_mkdir(self, *args, **kwargs)
    monkeypatch.setattr(Path, "mkdir", fail_mkdir)

    with caplog.at_level("WARNING"):
        logger = PayloadLogger(
            root=tmp_path / "_worker_payloads" / "test-worker",
            worker_id="test-worker", enabled=True,
        )

    # __init__ did not raise.
    assert logger.enabled is False  # self-disabled
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("payload_log" in r.message for r in warnings)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_payload_log.py::test_init_self_disables_on_mkdir_permission_error -v
```
Expected: failure (mkdir raises out of __init__)

- [ ] **Step 3: Wrap the mkdir**

In `PayloadLogger.__init__`, replace the unconditional mkdir with:

```python
        if self.enabled:
            try:
                self.root.mkdir(parents=True, exist_ok=True)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "payload_log: mkdir failed (%s: %s); disabling capture",
                    type(exc).__name__, exc,
                )
                self.enabled = False
```

- [ ] **Step 4: Run test**

```bash
pytest tests/test_payload_log.py -v
```
Expected: all passing

- [ ] **Step 5: Commit**

```bash
git add src/task_worker_api/payload_log.py tests/test_payload_log.py
git commit -m "feat(payload-log): __init__ self-disables on mkdir failure (never-raises)"
```

---

## Phase 2 — `BackendClient` integration

### Task 14: `BackendClient.__init__` accepts optional `payload_logger`

**Files:**
- Modify: `src/task_worker_api/client.py`
- Create: `tests/test_payload_log_integration.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_payload_log_integration.py
"""Integration tests that exercise paths FakeBackendClient cannot.

The protocol-drift capture lives inside the real BackendClient.claim_next,
so we mock the HTTP transport and use the real client. FakeBackendClient
bypasses claim_next entirely and would give us false confidence.
"""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from task_worker_api.client import BackendClient
from task_worker_api.enums import TaskType
from task_worker_api.payload_log import PayloadLogger


@pytest.mark.asyncio
async def test_backend_client_accepts_payload_logger(tmp_path: Path):
    logger = PayloadLogger(
        root=tmp_path / "_wp" / "w", worker_id="w", enabled=True,
        _boot_id="aaaaaaaa", _pid=lambda: 1,
    )
    client = BackendClient(
        "http://fake/api/v1", "key", payload_logger=logger,
    )
    assert client._payload_logger is logger
    await client.close()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_payload_log_integration.py -v
```
Expected: failure (`payload_logger` not accepted)

- [ ] **Step 3: Update `client.py`**

Add the import and parameter. Replace `BackendClient.__init__`:

```python
# at the top of client.py:
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .payload_log import PayloadLogger
```

```python
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout_s: float = 30.0,
        max_retries: int = 4,
        retry_backoff_s: float = 2.0,
        client: Optional[httpx.AsyncClient] = None,
        payload_logger: Optional["PayloadLogger"] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.max_retries = max_retries
        self.retry_backoff_s = retry_backoff_s
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout_s,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        self._payload_logger = payload_logger
```

- [ ] **Step 4: Run test**

```bash
pytest tests/test_payload_log_integration.py -v
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/task_worker_api/client.py tests/test_payload_log_integration.py
git commit -m "feat(client): BackendClient accepts optional payload_logger"
```

---

### Task 15: `claim_next` captures raw on JSON parse failure

**Files:**
- Modify: `src/task_worker_api/client.py`
- Modify: `tests/test_payload_log_integration.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_claim_next_captures_raw_on_json_parse_failure(tmp_path: Path):
    """Backend returns invalid JSON (e.g. a 500 HTML page with status 200).
    The raw response text and the JSONDecodeError must be captured."""
    root = tmp_path / "_wp" / "w"
    logger = PayloadLogger(
        root=root, worker_id="w", enabled=True,
        _boot_id="aaaaaaaa", _pid=lambda: 1,
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"<html><body>500 Internal Server Error</body></html>",
            headers={"content-type": "text/html"},
        )

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(
        base_url="http://fake/api/v1",
        transport=transport,
        headers={"Authorization": "Bearer x"},
    )
    client = BackendClient(
        "http://fake/api/v1", "x", client=http, payload_logger=logger,
    )

    with pytest.raises(Exception):
        await client.claim_next([TaskType.DETECT_CUT_PLANES], worker_id="w")

    await client.close()
    logger.close()

    raw_files = list(root.glob("raw_envelopes-*.jsonl"))
    assert len(raw_files) == 1
    import json
    entry = json.loads(raw_files[0].read_text(encoding="utf-8"))
    assert entry["error_type"] == "JSONDecodeError"
    assert "html" in entry["raw"].lower()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_payload_log_integration.py -v -k "json_parse"
```
Expected: failure (no raw record written)

- [ ] **Step 3: Update `claim_next`**

Replace `claim_next` in `client.py`:

```python
    async def claim_next(
        self, task_types: list, worker_id: str
    ) -> Optional[ClaimedTask]:
        """GET /tasks/next — claim the next available task. Returns None on 204."""
        types_str = ",".join(
            t.value if hasattr(t, "value") else str(t) for t in task_types
        )
        resp = await self._request(
            "GET", "/tasks/next",
            params={"types": types_str, "worker_id": worker_id},
        )
        if resp.status_code == 204:
            return None
        if resp.status_code == 404:
            log.warning("backend %s has no /tasks/next", self.base_url)
            return None
        resp.raise_for_status()

        try:
            body = resp.json()
        except Exception as exc:
            if self._payload_logger is not None:
                self._payload_logger.record_raw(
                    resp.text,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
            raise ProtocolError(
                f"claim_next response was not valid JSON: {resp.text[:500]!r}"
            ) from exc

        if body is None:
            return None

        try:
            return ClaimedTask.from_dict(body)
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            if self._payload_logger is not None:
                self._payload_logger.record_raw(
                    body,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
            raise ProtocolError(
                f"claim_next returned an unexpected envelope: {resp.text[:500]!r}"
            ) from exc
```

- [ ] **Step 4: Run test**

```bash
pytest tests/test_payload_log_integration.py -v -k "json_parse"
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/task_worker_api/client.py tests/test_payload_log_integration.py
git commit -m "feat(client): claim_next captures raw envelope on JSON parse failure"
```

---

### Task 16: `claim_next` captures raw on `from_dict` failure

**Files:**
- Modify: `tests/test_payload_log_integration.py`

Implementation done in Task 15. Add the second-failure-mode test.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_claim_next_captures_raw_on_from_dict_failure(tmp_path: Path):
    """Backend returns valid JSON but with an unknown task_type int —
    ClaimedTask.from_dict raises ValueError; raw envelope must be captured."""
    root = tmp_path / "_wp" / "w"
    logger = PayloadLogger(
        root=root, worker_id="w", enabled=True,
        _boot_id="aaaaaaaa", _pid=lambda: 1,
    )

    bad_body = {
        "id": 99,
        "task_type": "unknown_future_type",
        "status": 2,
        "case_id": 1,
        "item_key": "x",
        "params": {"k": "v"},
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=bad_body)

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(
        base_url="http://fake/api/v1", transport=transport,
        headers={"Authorization": "Bearer x"},
    )
    client = BackendClient(
        "http://fake/api/v1", "x", client=http, payload_logger=logger,
    )

    with pytest.raises(Exception):
        await client.claim_next([TaskType.DETECT_CUT_PLANES], worker_id="w")

    await client.close()
    logger.close()

    raw_files = list(root.glob("raw_envelopes-*.jsonl"))
    assert len(raw_files) == 1
    import json
    entry = json.loads(raw_files[0].read_text(encoding="utf-8"))
    assert entry["raw"] == bad_body
    assert entry["error_type"] == "ValueError"
    assert "unknown_future_type" in entry["error"]
```

- [ ] **Step 2: Run test**

```bash
pytest tests/test_payload_log_integration.py -v -k "from_dict"
```
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_payload_log_integration.py
git commit -m "test(client): claim_next raw capture on from_dict failure"
```

---

### Task 17: `claim_next` writes nothing on healthy no-claim response

**Files:**
- Modify: `tests/test_payload_log_integration.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_claim_next_no_raw_on_204(tmp_path: Path):
    """A healthy 204 (no task available) must not write any raw envelope."""
    root = tmp_path / "_wp" / "w"
    logger = PayloadLogger(
        root=root, worker_id="w", enabled=True,
        _boot_id="aaaaaaaa", _pid=lambda: 1,
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(
        base_url="http://fake/api/v1", transport=transport,
        headers={"Authorization": "Bearer x"},
    )
    client = BackendClient(
        "http://fake/api/v1", "x", client=http, payload_logger=logger,
    )

    result = await client.claim_next([TaskType.DETECT_CUT_PLANES], worker_id="w")
    assert result is None

    await client.close()
    logger.close()
    assert list(root.glob("raw_envelopes-*.jsonl")) == []
```

- [ ] **Step 2: Run test**

```bash
pytest tests/test_payload_log_integration.py -v -k "no_raw_on_204"
```
Expected: PASS (already correct from Task 15)

- [ ] **Step 3: Commit**

```bash
git add tests/test_payload_log_integration.py
git commit -m "test(client): claim_next no raw envelope on 204"
```

---

## Phase 3 — `Worker` integration

### Task 18: `Worker.__init__` constructs `PayloadLogger` with env-var parsing

**Files:**
- Modify: `src/task_worker_api/worker.py`
- Modify: `tests/test_worker_loop.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_worker_loop.py`:

```python
import os


@pytest.mark.asyncio
async def test_worker_constructs_payload_logger_when_shared_volume_set(tmp_path):
    fake = FakeBackendClient()
    worker = Worker(
        backend_url="http://fake/api/v1", api_key="k", worker_id="w",
        handlers={}, work_dir=str(tmp_path / "work"), client=fake,
        shared_volume_path=str(tmp_path / "shared"),
    )
    assert worker._payload_logger is not None
    assert worker._payload_logger.enabled is True
    assert (tmp_path / "shared" / "_worker_payloads" / "w").is_dir()


@pytest.mark.asyncio
async def test_worker_disabled_when_shared_volume_unset(tmp_path):
    """Existing tests rely on this — no shared_volume_path means no logger."""
    fake = FakeBackendClient()
    worker = Worker(
        backend_url="http://fake/api/v1", api_key="k", worker_id="w",
        handlers={}, work_dir=str(tmp_path / "work"), client=fake,
    )
    assert worker._payload_logger is not None
    assert worker._payload_logger.enabled is False


@pytest.mark.asyncio
async def test_worker_disabled_via_env_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKER_PAYLOAD_LOG_ENABLED", "false")
    fake = FakeBackendClient()
    worker = Worker(
        backend_url="http://fake/api/v1", api_key="k", worker_id="w",
        handlers={}, work_dir=str(tmp_path / "work"), client=fake,
        shared_volume_path=str(tmp_path / "shared"),
    )
    assert worker._payload_logger.enabled is False


@pytest.mark.asyncio
async def test_worker_retention_env_falls_back_on_bad_value(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("WORKER_PAYLOAD_LOG_RETENTION_DAYS", "abc")
    fake = FakeBackendClient()
    with caplog.at_level("WARNING"):
        worker = Worker(
            backend_url="http://fake/api/v1", api_key="k", worker_id="w",
            handlers={}, work_dir=str(tmp_path / "work"), client=fake,
            shared_volume_path=str(tmp_path / "shared"),
        )
    assert worker._payload_logger.retention_days == 14
    assert any("retention" in r.message.lower() for r in caplog.records)


@pytest.mark.asyncio
async def test_worker_retention_env_falls_back_on_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKER_PAYLOAD_LOG_RETENTION_DAYS", "0")
    fake = FakeBackendClient()
    worker = Worker(
        backend_url="http://fake/api/v1", api_key="k", worker_id="w",
        handlers={}, work_dir=str(tmp_path / "work"), client=fake,
        shared_volume_path=str(tmp_path / "shared"),
    )
    assert worker._payload_logger.retention_days == 14
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_worker_loop.py -v -k "payload_logger or disabled or retention_env"
```
Expected: failures (`AttributeError: '_payload_logger'`)

- [ ] **Step 3: Update `Worker.__init__`**

Add imports and update `__init__` in `worker.py`:

```python
# at top of worker.py
from .payload_log import PayloadLogger, sanitize_worker_id
```

In `Worker.__init__`, after the existing `self.shared_volume_path = ...` line and before `self._client = ...`:

```python
        self._payload_logger = self._build_payload_logger()
```

Add a new method on `Worker`:

```python
    def _build_payload_logger(self) -> PayloadLogger:
        """Construct a PayloadLogger from env + shared_volume_path.

        When shared_volume_path is None, the logger is constructed disabled —
        there's no place to write. When the env var WORKER_PAYLOAD_LOG_ENABLED
        is "false" (case-insensitive), disabled regardless. Bad retention env
        values fall back to 14 days with a WARNING.
        """
        env_enabled = (
            os.environ.get("WORKER_PAYLOAD_LOG_ENABLED", "true").lower() != "false"
        )
        enabled = bool(self.shared_volume_path) and env_enabled

        retention_raw = os.environ.get("WORKER_PAYLOAD_LOG_RETENTION_DAYS", "14")
        try:
            retention = int(retention_raw)
            if retention < 1:
                raise ValueError(f"retention must be >= 1, got {retention}")
        except (ValueError, TypeError):
            log.warning(
                "payload_log: WORKER_PAYLOAD_LOG_RETENTION_DAYS=%r is invalid; "
                "falling back to 14 days",
                retention_raw,
            )
            retention = 14

        if self.shared_volume_path:
            root = (
                Path(self.shared_volume_path)
                / "_worker_payloads"
                / sanitize_worker_id(self.worker_id)
            )
        else:
            # Placeholder path; logger is disabled so it'll never be touched.
            root = Path("/__payload_log_disabled__")

        return PayloadLogger(
            root=root,
            worker_id=self.worker_id,
            retention_days=retention,
            enabled=enabled,
        )
```

Then update the `BackendClient` construction so it threads the logger:

```python
        # In Worker.__init__, replace the existing self._client = ... line:
        if client is None:
            self._client = BackendClient(
                backend_url, api_key, timeout_s=request_timeout_s,
                payload_logger=self._payload_logger,
            )
        else:
            self._client = client
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_worker_loop.py -v
```
Expected: all passing (existing tests unaffected because they don't pass `shared_volume_path`)

- [ ] **Step 5: Commit**

```bash
git add src/task_worker_api/worker.py tests/test_worker_loop.py
git commit -m "feat(worker): construct PayloadLogger with env-var parsing + safe fallbacks"
```

---

### Task 19: `worker_id` sanitization at `Worker.__init__`

**Files:**
- Modify: `tests/test_worker_loop.py`

Implementation already covered by `sanitize_worker_id` call in Task 18; add the explicit assertion.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_worker_sanitizes_worker_id_in_log_path(tmp_path):
    """worker_id with slashes/.. must not escape into a sibling directory."""
    fake = FakeBackendClient()
    worker = Worker(
        backend_url="http://fake/api/v1", api_key="k",
        worker_id="../etc/passwd",
        handlers={}, work_dir=str(tmp_path / "work"), client=fake,
        shared_volume_path=str(tmp_path / "shared"),
    )
    # The created directory must live under _worker_payloads, not above it.
    children = list((tmp_path / "shared" / "_worker_payloads").iterdir())
    assert len(children) == 1
    sanitized = children[0].name
    assert ".." not in sanitized
    assert "/" not in sanitized
    assert "\\" not in sanitized
    assert "passwd" in sanitized  # original chars preserved post-sanitization
```

- [ ] **Step 2: Run test**

```bash
pytest tests/test_worker_loop.py::test_worker_sanitizes_worker_id_in_log_path -v
```
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_worker_loop.py
git commit -m "test(worker): sanitize worker_id when used as log path segment"
```

---

### Task 20: `Worker._run_one` records typed envelope before validation

**Files:**
- Modify: `src/task_worker_api/worker.py`
- Modify: `tests/test_worker_loop.py`

- [ ] **Step 1: Write the failing test**

```python
import json


@pytest.mark.asyncio
async def test_worker_writes_typed_record_on_happy_path(tmp_path):
    fake = FakeBackendClient()
    (tmp_path / "fake.stl").write_bytes(b"solid\nendsolid\n")
    fake.queue_task(
        task_type=TaskType.DETECT_CUT_PLANES,
        params={"input_path": str(tmp_path / "fake.stl"), "max_results": 3},
    )

    async def handler(ctx, params):
        return {"planes": []}

    shared = tmp_path / "shared"
    worker = Worker(
        backend_url="http://fake/api/v1", api_key="k", worker_id="w",
        handlers={TaskType.DETECT_CUT_PLANES: handler},
        work_dir=str(tmp_path / "work"), client=fake,
        shared_volume_path=str(shared),
    )
    await worker.run_one()
    worker._payload_logger.close()  # flush

    payload_dir = shared / "_worker_payloads" / "w"
    files = list(payload_dir.glob("payloads-*.jsonl"))
    assert len(files) == 1
    entry = json.loads(files[0].read_text(encoding="utf-8").strip())
    assert entry["task_type"] == "detect_cut_planes"
    assert entry["params"]["max_results"] == 3


@pytest.mark.asyncio
async def test_worker_writes_typed_record_even_on_schema_rejection(tmp_path):
    """Malformed payloads are exactly the bugs we most want to replay."""
    fake = FakeBackendClient()
    (tmp_path / "fake.stl").write_bytes(b"solid\nendsolid\n")
    fake.queue_task(
        task_type=TaskType.DETECT_CUT_PLANES,
        params={"input_path": str(tmp_path / "fake.stl"), "input_file": "oops"},
    )

    async def handler(ctx, params):
        raise AssertionError("must not run")

    shared = tmp_path / "shared"
    worker = Worker(
        backend_url="http://fake/api/v1", api_key="k", worker_id="w",
        handlers={TaskType.DETECT_CUT_PLANES: handler},
        work_dir=str(tmp_path / "work"), client=fake,
        shared_volume_path=str(shared),
    )
    await worker.run_one()
    worker._payload_logger.close()

    files = list((shared / "_worker_payloads" / "w").glob("payloads-*.jsonl"))
    entry = json.loads(files[0].read_text(encoding="utf-8").strip())
    # Captured BEFORE schema validation, so the bad field is preserved.
    assert entry["params"]["input_file"] == "oops"
    # And the task itself was failed:
    assert len(fake.failed_tasks) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_worker_loop.py -v -k "writes_typed_record"
```
Expected: 2 failures (no record yet)

- [ ] **Step 3: Update `_run_one`**

In `worker.py`, modify `_run_one`. The `record()` call must be the **first statement** of the `try:` block:

```python
    async def _run_one(self, task: ClaimedTask) -> None:
        """Stage inputs → run handler under heartbeat + cancel guard → publish."""
        task_dir = self.work_dir / f"task_{task.id}"
        progress = ProgressReporter(
            self._client, task.id,
            heartbeat_interval_s=self.heartbeat_interval_s,
        )

        try:
            self._payload_logger.record(task)  # capture BEFORE validation

            handler = self.handlers.get(task.task_type)
            if handler is None:
                raise ProtocolError(
                    f"no handler registered for task_type {task.task_type.value}"
                )
            # ... rest of the method unchanged
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_worker_loop.py -v
```
Expected: all passing

- [ ] **Step 5: Commit**

```bash
git add src/task_worker_api/worker.py tests/test_worker_loop.py
git commit -m "feat(worker): _run_one records typed envelope before schema validation"
```

---

### Task 21: `run_forever` startup INFO + cleanup + finally close

**Files:**
- Modify: `src/task_worker_api/worker.py`
- Modify: `tests/test_payload_log_integration.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_payload_log_integration.py
import asyncio
from task_worker_api.enums import TaskType
from task_worker_api.testing import FakeBackendClient
from task_worker_api.worker import Worker


async def _shutdown_after(worker: Worker, delay_s: float):
    await asyncio.sleep(delay_s)
    await worker.shutdown()


@pytest.mark.asyncio
async def test_run_forever_logs_startup_state(tmp_path: Path, caplog):
    fake = FakeBackendClient()
    shared = tmp_path / "shared"
    worker = Worker(
        backend_url="http://fake/api/v1", api_key="k", worker_id="w",
        handlers={}, work_dir=str(tmp_path / "work"), client=fake,
        shared_volume_path=str(shared),
        poll_interval_s=0.05,
    )

    asyncio.create_task(_shutdown_after(worker, 0.1))
    with caplog.at_level("INFO"):
        await asyncio.wait_for(worker.run_forever(), timeout=2.0)

    text = "\n".join(r.message for r in caplog.records)
    assert "payload logging" in text.lower()
    assert "enabled" in text.lower()


@pytest.mark.asyncio
async def test_run_forever_runs_startup_cleanup(tmp_path: Path):
    """Pre-existing expired files in the worker dir are gone after startup."""
    shared = tmp_path / "shared"
    worker_dir = shared / "_worker_payloads" / "w"
    worker_dir.mkdir(parents=True)
    expired = worker_dir / "payloads-2026-03-27-pid1-aaaa.jsonl"
    expired.write_text("{}\n", encoding="utf-8")
    import os, time
    age_s = 30 * 86400
    os.utime(expired, (time.time() - age_s, time.time() - age_s))

    fake = FakeBackendClient()
    worker = Worker(
        backend_url="http://fake/api/v1", api_key="k", worker_id="w",
        handlers={}, work_dir=str(tmp_path / "work"), client=fake,
        shared_volume_path=str(shared),
        poll_interval_s=0.05,
    )
    asyncio.create_task(_shutdown_after(worker, 0.1))
    await asyncio.wait_for(worker.run_forever(), timeout=2.0)

    assert not expired.exists()


@pytest.mark.asyncio
async def test_run_forever_closes_logger_in_finally(tmp_path: Path):
    fake = FakeBackendClient()
    shared = tmp_path / "shared"
    worker = Worker(
        backend_url="http://fake/api/v1", api_key="k", worker_id="w",
        handlers={}, work_dir=str(tmp_path / "work"), client=fake,
        shared_volume_path=str(shared),
        poll_interval_s=0.05,
    )
    asyncio.create_task(_shutdown_after(worker, 0.05))
    await asyncio.wait_for(worker.run_forever(), timeout=2.0)

    # After close, the logger's handles dict is empty.
    assert worker._payload_logger._handles == {}
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_payload_log_integration.py -v -k "run_forever"
```
Expected: failures (no startup INFO, no cleanup, no close)

- [ ] **Step 3: Update `run_forever`**

Replace `Worker.run_forever` in `worker.py`:

```python
    async def run_forever(self) -> None:
        """Main polling loop. Returns when shutdown() is called."""
        self.work_dir.mkdir(parents=True, exist_ok=True)
        log.info(
            "task-worker-api Worker starting: id=%s url=%s types=%s",
            self.worker_id, self.backend_url,
            ",".join(t.value for t in self.task_types),
        )
        if self._payload_logger.enabled:
            log.info(
                "payload logging: enabled, root=%s, retention=%dd",
                self._payload_logger.root, self._payload_logger.retention_days,
            )
        else:
            log.info(
                "payload logging: disabled (shared_volume_path=%r, env=%r)",
                self.shared_volume_path,
                os.environ.get("WORKER_PAYLOAD_LOG_ENABLED", "true"),
            )
        self._payload_logger.cleanup_old_files()

        try:
            while not self._stop.is_set():
                claimed = await self._claim()
                if claimed is None:
                    try:
                        await asyncio.wait_for(
                            self._stop.wait(),
                            timeout=self.poll_interval_s,
                        )
                    except asyncio.TimeoutError:
                        pass
                    continue
                await self._run_one(claimed)
        finally:
            self._payload_logger.close()
            await self._client.close()
            log.info("task-worker-api Worker stopped: id=%s", self.worker_id)
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_payload_log_integration.py -v
```
Expected: all passing

- [ ] **Step 5: Commit**

```bash
git add src/task_worker_api/worker.py tests/test_payload_log_integration.py
git commit -m "feat(worker): run_forever startup INFO + cleanup + finally close"
```

---

### Task 22: `run_forever` periodic cleanup timer

**Files:**
- Modify: `src/task_worker_api/worker.py`
- Modify: `tests/test_payload_log_integration.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_run_forever_periodic_cleanup(tmp_path: Path, monkeypatch):
    """With a tight cleanup interval, the timer fires multiple times during the run."""
    monkeypatch.setenv("WORKER_PAYLOAD_LOG_CLEANUP_INTERVAL_S", "0.05")
    fake = FakeBackendClient()
    shared = tmp_path / "shared"
    worker = Worker(
        backend_url="http://fake/api/v1", api_key="k", worker_id="w",
        handlers={}, work_dir=str(tmp_path / "work"), client=fake,
        shared_volume_path=str(shared),
        poll_interval_s=0.01,
    )

    counter = {"n": 0}
    real_cleanup = worker._payload_logger.cleanup_old_files
    def counting_cleanup():
        counter["n"] += 1
        real_cleanup()
    worker._payload_logger.cleanup_old_files = counting_cleanup  # type: ignore

    asyncio.create_task(_shutdown_after(worker, 0.3))
    await asyncio.wait_for(worker.run_forever(), timeout=2.0)

    # 1 startup + at least 2 periodic firings during 300ms with 50ms interval.
    assert counter["n"] >= 3
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_payload_log_integration.py::test_run_forever_periodic_cleanup -v
```
Expected: counter["n"] == 1 (only startup cleanup)

- [ ] **Step 3: Add the periodic cleanup loop**

In `worker.py`, add a new method:

```python
    async def _periodic_cleanup_loop(self, interval_s: float) -> None:
        try:
            while True:
                await asyncio.sleep(interval_s)
                if self._stop.is_set():
                    return
                self._payload_logger.cleanup_old_files()
        except asyncio.CancelledError:
            raise
```

Update `run_forever`:

```python
    async def run_forever(self) -> None:
        # ... startup unchanged through cleanup_old_files() ...

        cleanup_interval_s = float(
            os.environ.get("WORKER_PAYLOAD_LOG_CLEANUP_INTERVAL_S", "3600")
        )
        cleanup_task = asyncio.create_task(
            self._periodic_cleanup_loop(cleanup_interval_s)
        )

        try:
            while not self._stop.is_set():
                # ... loop body unchanged ...
        finally:
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass
            self._payload_logger.close()
            await self._client.close()
            log.info("task-worker-api Worker stopped: id=%s", self.worker_id)
```

- [ ] **Step 4: Run test**

```bash
pytest tests/test_payload_log_integration.py::test_run_forever_periodic_cleanup -v
```
Expected: PASS

- [ ] **Step 5: Run the full suite**

```bash
pytest -v
```
Expected: all passing (no regressions)

- [ ] **Step 6: Commit**

```bash
git add src/task_worker_api/worker.py tests/test_payload_log_integration.py
git commit -m "feat(worker): periodic cleanup timer keeps idle workers honest"
```

---

## Phase 4 — Polish

### Task 23: `docs/adding-a-worker.md` replay-transform subsection

**Files:**
- Modify: `docs/adding-a-worker.md`

- [ ] **Step 1: Locate insertion point**

```bash
grep -n "## " docs/adding-a-worker.md
```

Find a section near the end (e.g., after the deployment checklist or troubleshooting). The exact section depends on existing structure; insert before the final reference appendix if any.

- [ ] **Step 2: Append the new section**

Append this content at the end of `docs/adding-a-worker.md` (before any final reference / further-reading section):

```markdown
## Replaying captured payloads

`task-worker-api` v0.5.0+ captures every claimed task's full envelope to
`/app/shared/_worker_payloads/{worker_id}/payloads-YYYY-MM-DD-pidNNN-XXXX.jsonl`
inside the worker container. Use this when reproducing a worker bug locally
or replaying real producer traffic into a new feature's tests.

### What's in a captured line

Each line is a JSON object with both **task fields** (the original envelope
the backend sent) and **capture metadata** (process id, boot id, capture
timestamp). When re-enqueuing for replay, you must drop the capture
metadata — the backend will assign new values for those fields:

| Field          | Keep on replay? | Why |
|----------------|-----------------|-----|
| `task_type`    | yes             | required for re-enqueue |
| `case_id`      | yes             | task spec |
| `item_key`     | yes             | task spec |
| `params`       | yes             | task spec |
| `task_id`      | **drop**        | backend assigns a new id |
| `status`       | **drop**        | will be PENDING after re-enqueue |
| `worker_id`    | **drop**        | claim metadata, not part of the task spec |
| `captured_at`  | **drop**        | replay metadata |
| `stream`       | **drop**        | always `"typed"` |
| `process_id`   | **drop**        | replay metadata |
| `boot_id`      | **drop**        | replay metadata |

### Replay snippet

```python
# replay_payloads.py
import json
import sys
from pathlib import Path

from task_worker_api import BackendClient, TaskType

REPLAY_KEEP_FIELDS = {"task_type", "case_id", "item_key", "params"}

async def replay(jsonl_path: Path, backend_url: str, api_key: str) -> None:
    async with BackendClient(backend_url, api_key) as client:
        for line in jsonl_path.read_text(encoding="utf-8").splitlines():
            envelope = json.loads(line)
            if envelope.get("stream") != "typed":
                continue  # skip raw_envelopes lines
            spec = {k: v for k, v in envelope.items() if k in REPLAY_KEEP_FIELDS}
            spec["task_type"] = TaskType(spec["task_type"])
            await client.enqueue(**spec)  # use your backend's enqueue method

if __name__ == "__main__":
    import asyncio
    asyncio.run(replay(Path(sys.argv[1]), sys.argv[2], sys.argv[3]))
```

### Disabling capture

Set `WORKER_PAYLOAD_LOG_ENABLED=false` in the worker container's environment.
Tune retention with `WORKER_PAYLOAD_LOG_RETENTION_DAYS` (default 14).
```

- [ ] **Step 3: Verify the markdown renders**

```bash
grep -n "Replaying captured payloads" docs/adding-a-worker.md
```
Expected: one matching line near the end of the file.

- [ ] **Step 4: Commit**

```bash
git add docs/adding-a-worker.md
git commit -m "docs: add replaying captured payloads guide for v0.5.0"
```

---

### Task 24: Version bump + CHANGELOG entry + `__init__.py` re-export check

**Files:**
- Modify: `src/task_worker_api/__init__.py`
- Modify: `pyproject.toml`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Verify `PayloadLogger` is NOT in `__all__`**

```bash
grep -n "PayloadLogger\|sanitize_worker_id" src/task_worker_api/__init__.py
```
Expected: no matches. The class is internal; consumers go through `Worker`.

- [ ] **Step 2: Bump `__version__`**

Edit `src/task_worker_api/__init__.py`:

```python
__version__ = "0.5.0"
```
(replacing the existing `"0.4.1"`)

- [ ] **Step 3: Bump `pyproject.toml` version**

Edit `pyproject.toml`:

```toml
version = "0.5.0"
```
(replacing the existing `"0.4.1"`)

- [ ] **Step 4: Add CHANGELOG entry**

Insert at the top of `CHANGELOG.md` after the `# Changelog` heading:

```markdown
## v0.5.0 — 2026-04-26

Adds per-worker payload logging — every claimed task's full envelope is
captured to JSONL inside the worker container so an operator can reproduce
a worker bug or replay producer traffic into tests without rebuilding
payloads by hand.

**New:**
- `PayloadLogger` (internal) writes two streams under
  `/app/shared/_worker_payloads/{worker_id}/`:
  - `payloads-DATE-pidPID-BOOT.jsonl` — one line per claimed task,
    captured before schema validation.
  - `raw_envelopes-DATE-pidPID-BOOT.jsonl` — captured by `BackendClient`
    when `ClaimedTask.from_dict()` or `response.json()` raises (protocol
    drift between backend and worker schema).
- Daily UTC rotation. Per-process file naming (PID + 8-char boot id) so
  scaled replicas with one shared `WORKER_ID` don't corrupt JSONL via
  interleaved writes.
- Default 14-day retention via `WORKER_PAYLOAD_LOG_RETENTION_DAYS`.
  Cleanup runs at startup, on UTC date rollover, and on a periodic
  timer (`WORKER_PAYLOAD_LOG_CLEANUP_INTERVAL_S`, default 3600s).
  Cleanup runs even when the logger is disabled, so a kill-switch
  deployment doesn't accumulate logs forever.
- 256KB per-record cap with two-stage truncation (224KB on the
  variable-size field; full-record check after construction for
  pathological non-payload fields).
- Default-on. Disable per deployment with
  `WORKER_PAYLOAD_LOG_ENABLED=false`.

**Failure contract:** `PayloadLogger` (including `__init__`) never
raises. Disk full, fs flap, permission errors, or unserialisable
values produce one WARNING log per process lifetime; subsequent
failures are silent. Worker keeps polling and running tasks.

**Worker integration:** `Worker.__init__` constructs the logger when
`shared_volume_path` is set, parses env vars with safe fallbacks for
bad values, sanitises `worker_id` for path safety (Windows reserved
names, slashes, `..`), and wires the logger into `BackendClient` only
when the SDK constructs the client itself. Externally-supplied
clients (e.g., `FakeBackendClient`) are not modified.

**Tests:** new `tests/test_payload_log.py` (pure unit) and
`tests/test_payload_log_integration.py` (real `BackendClient` +
`httpx.MockTransport`, plus `Worker.run_forever` startup/finally).

**Docs:** `docs/adding-a-worker.md` gains a "Replaying captured
payloads" section with a runnable transform that drops claim
metadata before re-enqueueing.

**Deployment-side (separate PR in `syngar-deployment-scripts/surgiclaw`):**
add `WORKER_PAYLOAD_LOG_ENABLED` and `WORKER_PAYLOAD_LOG_RETENTION_DAYS`
to `.env`, `.env.linux`, `.env.example`, and to each worker service's
`environment:` block in `docker-compose.yml`. No volume changes —
the existing `${SHARED_DATA_PATH}:/app/shared` mount is reused.
```

- [ ] **Step 5: Verify version pickup**

```bash
python -c "import sys; sys.path.insert(0, 'src'); import task_worker_api; print(task_worker_api.__version__)"
```
Expected: `0.5.0`

- [ ] **Step 6: Run full test suite**

```bash
pytest -v
```
Expected: all passing

- [ ] **Step 7: Commit**

```bash
git add src/task_worker_api/__init__.py pyproject.toml CHANGELOG.md
git commit -m "chore: bump to v0.5.0 — payload logging release"
```

---

## Phase 5 — Deployment-side PR (`syngar-deployment-scripts/surgiclaw`)

> Ships as a **separate PR** in the deployment repo. Land after the SDK release wheel is published. The SDK gates the feature behind env vars, so this PR can land before all worker images upgrade — workers without the new SDK simply ignore the new env vars.

### Task 25: Deployment env files + compose wiring

**Files:**
- Modify: `P:/Project/syngar-deployment-scripts/surgiclaw/.env.example`
- Modify: `P:/Project/syngar-deployment-scripts/surgiclaw/.env`
- Modify: `P:/Project/syngar-deployment-scripts/surgiclaw/.env.linux`
- Modify: `P:/Project/syngar-deployment-scripts/surgiclaw/docker-compose.yml`

- [ ] **Step 1: Add the env block to `.env.example`**

Insert near the worker section (around line 100, between the `WORKER_API_KEYS=` block and the `BLENDER_WORKER` block):

```bash

# ----- Worker payload logging --------------------------------------
# Captures every claimed task's full envelope to
# /app/shared/_worker_payloads/{worker_id}/payloads-YYYY-MM-DD-pidNNN-XXXX.jsonl
# inside each worker container. Used to reproduce worker bugs and
# replay real producer traffic into tests. Files older than the
# retention window are deleted on worker startup, on UTC date
# rollover, and on a periodic timer.
WORKER_PAYLOAD_LOG_ENABLED=true
WORKER_PAYLOAD_LOG_RETENTION_DAYS=14
```

- [ ] **Step 2: Add the same block to `.env`**

Same text as Step 1.

- [ ] **Step 3: Add the same block to `.env.linux`**

Same text as Step 1.

- [ ] **Step 4: Wire env vars into each worker service in `docker-compose.yml`**

Add these two lines to the `environment:` block of `neural-canvas`, `blender-worker`, and `colmap-splat-worker` services (next to the existing `WORKER_*` env vars):

```yaml
      - WORKER_PAYLOAD_LOG_ENABLED=${WORKER_PAYLOAD_LOG_ENABLED:-true}
      - WORKER_PAYLOAD_LOG_RETENTION_DAYS=${WORKER_PAYLOAD_LOG_RETENTION_DAYS:-14}
```

No `volumes:` change — the existing `${SHARED_DATA_PATH}:/app/shared` mount is what the SDK writes into.

- [ ] **Step 5: Validate compose syntax**

```bash
cd P:/Project/syngar-deployment-scripts/surgiclaw
docker compose config > /dev/null
```
Expected: no errors. (If `docker compose config` is unavailable on the workstation, skip and rely on CI.)

- [ ] **Step 6: Commit**

```bash
cd P:/Project/syngar-deployment-scripts/surgiclaw
git add .env .env.linux .env.example docker-compose.yml
git commit -m "feat(workers): enable task-worker-api v0.5.0 payload logging

Adds WORKER_PAYLOAD_LOG_ENABLED + WORKER_PAYLOAD_LOG_RETENTION_DAYS to
all three worker services (neural-canvas, blender-worker, colmap-splat-worker).
Default-on; flip to false per deployment if compliance disagrees.
Files land under /app/shared/_worker_payloads/{worker_id}/ on the
existing shared volume — no mount changes needed."
```

---

## Phase 6 — Worker-repo audit (prerequisite for the deployment PR to actually take effect)

> Without this audit, the env-var knob is attached to nothing — the SDK silently disables the logger when `shared_volume_path` is `None`.

### Task 26: Audit each worker repo's `Worker(...)` construction

**Files (READ ONLY — verification, may produce small follow-up PRs in each repo):**
- `blender-worker/src/sdk_worker.py` (or equivalent entry point)
- `colmap-splat-worker/src/sdk_worker.py` (or equivalent)
- `Neural-Canvas/src/sdk_worker.py` (Neural-Canvas in hybrid mode)

For each repo:

- [ ] **Step 1: Find the Worker constructor call**

```bash
cd <worker-repo>
grep -rn "Worker(" src/
```

- [ ] **Step 2: Verify `shared_volume_path` is passed**

The construction should look like:

```python
worker = Worker(
    backend_url=...,
    api_key=...,
    worker_id=...,
    handlers={...},
    shared_volume_path=os.environ.get("SHARED_VOLUME_PATH"),
    ...
)
```

If the line is missing or commented out, add it (matching the pattern used by other repos).

- [ ] **Step 3: Verify the env var is set in the container**

```bash
grep -n "SHARED_VOLUME_PATH" <worker-repo>/Dockerfile <worker-repo>/docker-compose*.yml
```

Surgiclaw's `docker-compose.yml` already sets `SHARED_VOLUME_PATH=/app/shared` for `blender-worker` and `colmap-splat-worker`. `neural-canvas` may differ — check whichever entry point it uses.

- [ ] **Step 4: If any repo needs a code change, file a follow-up PR**

Each follow-up PR is small (one line in `sdk_worker.py` plus a `task-worker-api>=0.5.0` dep bump in `pyproject.toml`).

- [ ] **Step 5: After all worker images are rebuilt and pushed, deploy the surgiclaw PR from Phase 5.**

---

## Self-Review

After writing the plan, verified:

**1. Spec coverage:**
- ✅ Two streams (typed + raw_envelopes): Tasks 4, 9
- ✅ Per-process file naming with 8-char boot_id: Tasks 2, 3
- ✅ UTC date rotation, idle-across-days: Task 5
- ✅ `default=str` for non-serializable params: Task 6
- ✅ Two-stage 256KB cap: Task 7
- ✅ Never-raises contract + degraded retry: Tasks 8, 13
- ✅ `cleanup_old_files` retention boundary: Task 10
- ✅ Cleanup tolerates PermissionError, runs while disabled: Task 11
- ✅ `close()` idempotent: Task 12
- ✅ `__init__` self-disables on mkdir failure: Task 13
- ✅ `BackendClient.payload_logger` keyword: Task 14
- ✅ `claim_next` raw capture on JSON parse failure: Task 15
- ✅ `claim_next` raw capture on `from_dict` failure: Task 16
- ✅ `Worker.__init__` env-var parsing with safe fallback: Task 18
- ✅ `worker_id` sanitization: Task 19
- ✅ `_run_one` records before validation: Task 20
- ✅ `run_forever` INFO + cleanup + close in finally: Task 21
- ✅ Periodic cleanup timer: Task 22
- ✅ Replay-transform docs: Task 23
- ✅ Version bump + CHANGELOG: Task 24
- ✅ Deployment env files + compose: Task 25
- ✅ Worker-repo audit: Task 26

**2. Placeholder scan:** none — every step has concrete code or commands.

**3. Type consistency:** `PayloadLogger.record(task)`, `record_raw(raw, *, error_type, error)`, `cleanup_old_files()`, `close()` consistent across tasks. `_payload_logger` attribute name consistent across `Worker` and `BackendClient`.

**4. Test framework:** pytest + pytest-asyncio (already in `pyproject.toml` dev deps). `httpx.MockTransport` is built-in to httpx — no new dependency needed.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-04-26-payload-logging.md`. Two execution options:

**1. Subagent-Driven (recommended)** — fresh subagent per task, review between tasks, fast iteration on a 26-task plan.

**2. Inline Execution** — execute tasks in this session using `superpowers:executing-plans`, batch execution with checkpoints.

**Which approach?**
