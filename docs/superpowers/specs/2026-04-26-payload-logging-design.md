# Worker Payload Logging — Design

**Status:** draft, pending implementation
**Target version:** task-worker-api v0.5.0
**Date:** 2026-04-26

## Goal

Capture every claimed task's full envelope to a local file inside each worker container, so an operator can reproduce a worker issue or feed real producer traffic into a new feature's tests without rebuilding payloads by hand.

## Non-goals

- Capturing task **results** (already in `task.result` after `complete()`).
- Capturing failure tracebacks (already in `task.error` after `fail()`).
- Centralised log shipping. Files stay on each worker's mounted volume; ops collects them out-of-band when needed.
- Redaction. The privacy boundary matches the existing `/app/shared/case_data` volume, which already holds the same case data.

## What gets logged

The full claim envelope — equivalent to `ClaimedTask` plus a capture timestamp and the writer process's PID/boot ID for traceability. One JSON object per task, written as one line of JSONL:

```jsonl
{"captured_at":"2026-04-26T14:23:11.234567Z","task_id":12345,"task_type":"cinematic_baking","case_id":99,"item_key":"scene_01","status":2,"params":{...},"worker_id":"blender-worker-1","process_id":12345,"boot_id":"a1b2"}
```

Capture happens **immediately after claim, before schema validation**. Schema-validation failures are exactly the bugs most worth replaying, so the record must land on disk even when `params_schema(**task.params)` would raise.

## Where files live

Path inside each worker container: `/app/shared/_worker_payloads/{worker_id}/`.

Reuses the existing `${SHARED_DATA_PATH}:/app/shared` bind mount that every worker service in `surgiclaw/docker-compose.yml` already declares. Bind mounts work identically on Linux Docker and Windows Docker Desktop without extra `.env` plumbing for ops.

The leading underscore on `_worker_payloads` flags the directory as non-case-data so any future cleanup or backup logic that walks `/app/shared` can skip it by convention.

## File naming and rotation

Pattern: `payloads-{YYYY-MM-DD}-pid{PID}-{BOOT_ID}.jsonl` (UTC date).

Examples:
- `payloads-2026-04-26-pid12345-a1b2.jsonl`
- `payloads-2026-04-26-pid12346-a1b2.jsonl` (sibling replica, same WORKER_ID, different PID)

`BOOT_ID` is a 4-character hex random suffix generated once at `PayloadLogger.__init__`. Combining PID + boot ID makes the filename unique across PID recycling within the retention window, so scaled replicas (`docker compose up --scale colmap-splat-worker=N`, all inheriting one `WORKER_ID`) cannot interleave partial JSON lines into the same file. Atomic-append on the WSL2 9P mount is unreliable for writes larger than `PIPE_BUF`; per-process files sidestep the problem entirely.

Rotation: when the UTC date changes, close the current file and open a new one on the next write.

Reading back is `cat *.jsonl | jq -c` or equivalent — every consumer treats the directory as one logical stream.

## Retention

Time-based, configurable via `WORKER_PAYLOAD_LOG_RETENTION_DAYS` (default `14`).

On `Worker.run_forever` startup, before the polling loop begins, the logger scans `_worker_payloads/{worker_id}/payloads-*.jsonl` and removes any file whose `mtime` is older than `now - retention_days`. Cleanup runs once per process start; not on every write.

Self-cleaning is required: a forgotten worker filling the shared volume would break case_data writes (a real outage), so retention can't be left to ops cron.

## Default behaviour and opt-out

On by default. Disable per deployment with `WORKER_PAYLOAD_LOG_ENABLED=false`.

The whole point of the feature is "have the evidence when something breaks." Opt-in defeats that; the most-debug-worthy failures are the ones nobody saw coming. Disk cost is bounded by retention, and the file lives inside the same trust boundary as `case_data`.

## Failure mode

`PayloadLogger.record(task)` **must never raise**. If the disk is full, the directory is unwritable, or the filesystem flaps:

1. Log a single `WARNING` via the `task_worker_api.payload_log` logger, including the underlying exception.
2. Mark the logger as degraded so subsequent failures don't spam logs (one warning per worker process lifetime).
3. Return without raising. The worker keeps polling and running tasks.

Payload logging is a debug aid, not a correctness feature. A failure here must not block task execution or fail tasks that would otherwise succeed.

## SDK changes

### New module: `src/task_worker_api/payload_log.py`

```python
class PayloadLogger:
    def __init__(
        self,
        *,
        root: Path,                # /app/shared/_worker_payloads/{worker_id}/
        worker_id: str,
        retention_days: int = 14,
        enabled: bool = True,
    ) -> None: ...

    def cleanup_old_files(self) -> None:
        """Scan root dir, delete files older than retention_days. Called once
        at worker startup. Never raises."""

    def record(self, task: ClaimedTask) -> None:
        """Append one JSON line for this task. Never raises. No-op when
        disabled. Rotates file on UTC date change."""
```

State held: current open file handle, current UTC date, boot_id (4-char hex), degraded flag (set after first I/O failure to suppress repeat warnings).

The file handle is held open across `record()` calls to avoid open/close overhead per task. After each `write()`, the logger calls `flush()` so a process crash loses at most the in-flight task. On UTC date rollover, the old handle is closed before the new one opens. A `close()` method is provided and called from `Worker.run_forever`'s `finally` block to release the handle on graceful shutdown.

I/O is synchronous — one append per task, microseconds, on a polling loop that already does HTTP round-trips. Async wrapping would add complexity for no measurable gain.

For testability, the logger accepts injectable seams as private keyword args: `_now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)`, `_pid: Callable[[], int] = os.getpid`, `_boot_id: Optional[str] = None` (defaults to a fresh 4-char hex). Tests inject these to drive date rollover deterministically and to fix PID/boot_id for filename-uniqueness assertions, without `freezegun` or real subprocesses.

### `Worker` integration (`src/task_worker_api/worker.py`)

1. `Worker.__init__` constructs a `PayloadLogger` from:
   - `root`: `Path(shared_volume_path) / "_worker_payloads" / worker_id` (only when `shared_volume_path` is set; otherwise the logger is constructed disabled — no shared mount, no place to write).
   - `worker_id`: `self.worker_id`.
   - `retention_days`: `int(os.environ.get("WORKER_PAYLOAD_LOG_RETENTION_DAYS", "14"))`.
   - `enabled`: `os.environ.get("WORKER_PAYLOAD_LOG_ENABLED", "true").lower() != "false"`.

2. `Worker.run_forever` calls `self._payload_logger.cleanup_old_files()` once after `self.work_dir.mkdir(...)` and before entering the polling loop.

3. `Worker._run_one` calls `self._payload_logger.record(task)` as the **first line** of the `try:` block — before `params_schema(**task.params)`, so malformed-payload tasks still produce a record.

### Public API additions

`PayloadLogger` is **not** added to `__all__`. It's an internal collaborator the `Worker` owns. Exposing it would invite consumers to instantiate it directly, fragmenting the contract; keeping it private means future format/path changes are non-breaking.

### Version bump

`__version__` → `"0.5.0"`. CHANGELOG entry under a new `## [0.5.0]` heading.

## Tests (`tests/test_payload_log.py`)

- **Round-trip:** `record()` writes a parseable JSON object; reading the line back yields the same envelope (modulo `captured_at`/`process_id`/`boot_id`).
- **Date rotation:** drive the injected `_now` callable across UTC midnight, assert that the second `record()` opens a new file with the next date in the name and that the previous handle is closed.
- **Retention cleanup:** create files with `os.utime` set to 7 / 14 / 30 days ago, run `cleanup_old_files(retention_days=14)`, assert the 30-day-old file is gone and the 7-day file remains. The 14-day file's behaviour is the boundary — implementation deletes when `age >= retention_days`, asserted explicitly.
- **Filename uniqueness across processes:** instantiate two `PayloadLogger`s with the same `worker_id` but different injected PIDs/boot IDs (no real subprocess needed — the design avoids races by construction, so the test only needs to prove the filenames diverge). Each writes 100 records; assert both files exist with 100 valid JSON lines each, and that the directory contains exactly two files.
- **Disabled flag short-circuits:** `enabled=False` writes nothing, raises nothing, and `cleanup_old_files()` is a no-op.
- **Degraded mode:** monkeypatch the file open to raise `OSError`; first call logs a WARNING, subsequent calls are silent; `record()` returns normally in all cases. Worker loop must keep running.
- **Captured before validation:** integration-style test against `Worker._run_one` with a malformed `params` dict; assert the JSONL file contains the bad payload before the task transitions to FAILED.

`tests/test_worker_loop.py` gains one assertion that `record()` was called for the claimed task.

## Deployment-side changes (`syngar-deployment-scripts/surgiclaw`)

These are tracked here because the feature is unusable without them, but they live in a separate repo and ship as a separate PR.

### `.env.example`

Add a new `# ----- Worker payload logging -----` block, near the worker block (~line 100), containing:

```bash
# Worker payload logging — captures every claimed task's full envelope to
# /app/shared/_worker_payloads/{worker_id}/payloads-YYYY-MM-DD-pidNNN-XXXX.jsonl
# inside each worker container. Used to reproduce worker bugs and replay
# real producer traffic into tests. Files older than the retention window
# are deleted on worker startup.
WORKER_PAYLOAD_LOG_ENABLED=true
WORKER_PAYLOAD_LOG_RETENTION_DAYS=14
```

### `.env`, `.env.linux`

Add the same two keys with the same defaults. (Operator's local `.env` may override per deployment; `.env.linux` mirrors `.env.example` so a fresh Linux clone works without manual edits.)

### `docker-compose.yml`

Add to each worker service's `environment:` block (`neural-canvas`, `blender-worker`, `colmap-splat-worker`):

```yaml
- WORKER_PAYLOAD_LOG_ENABLED=${WORKER_PAYLOAD_LOG_ENABLED:-true}
- WORKER_PAYLOAD_LOG_RETENTION_DAYS=${WORKER_PAYLOAD_LOG_RETENTION_DAYS:-14}
```

No volume changes needed — the existing `${SHARED_DATA_PATH}:/app/shared` mount is what the SDK writes into.

### `docker-compose.linux.yml`

No changes. The override file currently only patches `nexus-core`; worker services inherit `environment:` from the base compose unchanged.

## Order of work

1. SDK PR: `payload_log.py` module + `Worker` integration + tests + CHANGELOG + version bump to v0.5.0. Merge and publish wheel via existing GitHub Releases workflow.
2. Worker repos (`blender-worker`, `colmap-splat-worker`, `neural-canvas`): bump `task-worker-api` dependency to `>=0.5.0`. No code changes — they consume the SDK transparently.
3. Deployment PR (`syngar-deployment-scripts/surgiclaw`): `.env`, `.env.linux`, `.env.example`, `docker-compose.yml`. Pull updated worker images. Deploy.

Steps 2 and 3 can land in either order; the SDK gates the feature behind the env var, so deploying the compose change before the worker images upgrade is a no-op rather than a regression.
