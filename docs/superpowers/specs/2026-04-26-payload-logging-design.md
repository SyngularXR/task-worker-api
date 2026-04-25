# Worker Payload Logging — Design

**Status:** review-complete (gstack-plan-eng-review + outside voice), pending implementation
**Target version:** task-worker-api v0.5.0
**Date:** 2026-04-26 (revised after engineering review)

## Goal

Capture every claimed task's full envelope to a local file inside each worker container, so an operator can reproduce a worker issue or feed real producer traffic into a new feature's tests without rebuilding payloads by hand.

## NOT in scope

- **Capturing task results.** Already in `task.result` after `complete()`.
- **Capturing failure tracebacks.** Already in `task.error` after `fail()`.
- **Centralised log shipping.** Files stay on each worker's mounted volume; ops collects them out-of-band when needed.
- **Redaction.** The privacy boundary matches the existing `/app/shared/case_data` volume, which already holds the same case data.
- **Backend-side `GET /tasks/{id}/envelope` endpoint.** Considered: the backend already stores `task.params` per row, so a server-side read endpoint would solve "reproduce a payload" with zero per-worker disk and works for tasks the worker never claimed. Rejected for this scope because (a) the user explicitly requested in-container capture so debugging works without backend round-trips or backend cooperation, (b) the task-worker-api SDK ships to consumers who run their own backend, and we can't assume the backend exposes such an endpoint, (c) it's a separate feature in a different repo. Logged as a TODO for SynPusher-Vue's Nexus Core.
- **Daily file size cap with continuation suffix.** Per-record cap (256KB) bounds typical growth; a runaway producer could still accumulate within retention but is far from the most likely failure. Defer to v2 if real volume warrants.
- **Backfilling logs across worker restarts.** A new boot ID per process means each restart starts a new file; we don't reconcile records across restarts.

## What already exists

- `shared_volume_path` is plumbed end-to-end (`Worker.__init__` → `Worker._run_one` → `upload_outputs` → `shared/temp/{task_id}/`). v0.4.1 established the `shared/<subdir>/` convention; this feature adds `shared/_worker_payloads/{worker_id}/`.
- Python `logging` is configured in `worker.py`. The simpler alternative (`log.info("claimed: %s", task.params)`) was rejected because Docker's json-file driver is capped at 30MB total per container, isn't grep-friendly across days, and isn't replayable as JSONL.
- `Worker._run_one` already has `task.params` validated and ready in scope at the right point for capture.
- The existing GitHub Releases workflow publishes a wheel on every main merge — no new distribution work needed.

## What gets logged

Two streams in the same per-worker directory:

**1. `payloads-{YYYY-MM-DD}-pid{PID}-{BOOT_ID}.jsonl`** — typed envelope, one line per task, captured in `Worker._run_one` immediately after claim and before `params_schema(**task.params)`. Schema-validation failures still produce a record.

```jsonl
{"captured_at":"2026-04-26T14:23:11.234567Z","stream":"typed","task_id":12345,"task_type":"cinematic_baking","case_id":99,"item_key":"scene_01","status":2,"params":{...},"worker_id":"blender-worker-1","process_id":12345,"boot_id":"a1b2c3d4"}
```

**2. `raw_envelopes-{YYYY-MM-DD}-pid{PID}-{BOOT_ID}.jsonl`** — raw response dict written in `BackendClient.claim_next` only when `ClaimedTask.from_dict()` raises (protocol-drift bugs, e.g., backend deploys a new task_type before workers update). Empty file on healthy days.

```jsonl
{"captured_at":"2026-04-26T14:23:11.234567Z","stream":"raw","raw":{...},"error":"ValueError: 99 is not a valid TaskType","worker_id":"blender-worker-1","process_id":12345,"boot_id":"a1b2c3d4"}
```

Two streams (instead of one mixed file) so the typed stream is directly replayable line-by-line without filtering, while the raw stream stays scannable for "did the backend deploy break us."

## Where files live

Path inside each worker container: `/app/shared/_worker_payloads/{worker_id}/`.

Reuses the existing `${SHARED_DATA_PATH}:/app/shared` bind mount that every worker service in `surgiclaw/docker-compose.yml` already declares. Bind mounts work identically on Linux Docker and Windows Docker Desktop without extra `.env` plumbing for ops. The leading underscore on `_worker_payloads` flags the directory as non-case-data so any future cleanup or backup logic walking `/app/shared` can skip it by convention.

If `shared_volume_path` is not provided to `Worker.__init__` (e.g., a worker running off a local-mode `WORKER_WORKDIR` only), the logger is constructed disabled. **`Worker.run_forever` emits one `INFO` line at startup stating whether the logger is enabled and, if disabled, why** — so operators don't discover the feature didn't run only when they need it.

## File naming and rotation

Pattern: `{stream}-{YYYY-MM-DD}-pid{PID}-{BOOT_ID}.jsonl` (UTC date), where `stream` is `payloads` or `raw_envelopes`.

`BOOT_ID` is `uuid.uuid4().hex[:8]` (8 hex chars / 32 bits) generated once at `PayloadLogger.__init__`. Combining PID + boot ID makes the filename unique across PID recycling within the retention window; 8 hex chars (vs the previously specified 4) avoids birthday collisions on redeploys with N replicas. The name "boot_id" is a misnomer in that it doesn't survive process restart — it's a per-process unique suffix.

Rotation logic — explicit: at the **start of every** `record()` and `record_raw()` call, the logger compares `_now().date()` to the cached `current_date`. If different, it closes the current handle and opens a new file with the new date. This handles the "worker idle across midnight, writes one record at 00:00:01" case correctly. A worker that processes one task at 23:59:59 and another at 00:00:30 lands them in their respective day's files.

Per-process files (rather than a single shared file per worker_id) sidestep multi-process append corruption: scaled replicas (`docker compose up --scale colmap-splat-worker=N`, all inheriting one `WORKER_ID`) cannot interleave partial JSON lines. Atomic-append on the WSL2 9P bind mount on Windows Docker Desktop is unreliable for writes larger than `PIPE_BUF`; per-process files remove the requirement.

Reading back across all files for a given day: `cat *.jsonl | jq -c` or equivalent.

## Retention

Time-based, configurable via `WORKER_PAYLOAD_LOG_RETENTION_DAYS` (default `14`).

Cleanup runs:
1. **At `Worker.run_forever` startup**, before the polling loop.
2. **On UTC date rollover**, immediately after the new day's file is opened.

Running on rollover (and not just startup) is what keeps long-lived workers honest. A worker running for 30 days with 14-day retention without rollover-triggered cleanup would accumulate 30 days of files; with rollover-triggered cleanup it never has more than `retention_days + 1` per stream per process.

Cleanup scans `_worker_payloads/{worker_id}/{payloads,raw_envelopes}-*.jsonl` and removes files where `now - mtime >= retention_days`. Boundary is inclusive on `==`. Each `os.remove` is wrapped in try/except: when two replicas race on the same expired file, the second's `FileNotFoundError` is swallowed and processing continues. Cleanup never raises out of the logger.

## Per-record size cap

Cap each record at **256KB** (UTF-8 encoded JSON length). If a record would exceed the cap:
1. Replace the offending field (`params` for typed; `raw` for raw-envelope) with `{"_truncated": true, "_original_size_bytes": N}`.
2. Re-serialize and write the truncated record.
3. On the **first** truncation per process lifetime, log a `WARNING` naming the task and original size. Subsequent truncations are silent.

The cap protects against "50MB params dict × hundreds of tasks/day × 14 days = unbounded disk." Realistic params are <10KB; truncation is exception, not norm.

## Default behaviour and opt-out

On by default. Disable per deployment with `WORKER_PAYLOAD_LOG_ENABLED=false`.

The whole point is "have the evidence when something breaks" — opt-in defeats that. Disk cost is bounded by retention + per-record cap, and the file lives inside the same trust boundary as `case_data`. The medical (surgiclaw) deployment can opt out via `.env` if compliance disagrees; SDK ships sensible defaults, deployment-side overrides handle policy.

## Failure mode

`PayloadLogger.record()`, `.record_raw()`, `.cleanup_old_files()`, and `.close()` **must never raise**. If the disk is full, the directory is unwritable, the filesystem flaps, or `json.dumps` chokes on a value that even `default=str` can't serialize:

1. Log a single `WARNING` via the `task_worker_api.payload_log` logger, including the underlying exception.
2. Mark the logger as degraded so subsequent failures don't spam logs (one warning per worker process lifetime).
3. Return without raising. The worker keeps polling and running tasks.

Durability: `flush()` is called after every write. A Python-process crash loses at most the in-flight task. A kernel panic, Docker Desktop VM crash, or host power loss can lose more — `fsync` is **not** called per record because the latency cost outweighs the rarity of the loss class. Payload logging is a debug aid, not a correctness feature.

## SDK changes

### New module: `src/task_worker_api/payload_log.py`

```python
class PayloadLogger:
    def __init__(
        self,
        *,
        root: Path,                     # /app/shared/_worker_payloads/{worker_id}/
        worker_id: str,
        retention_days: int = 14,
        enabled: bool = True,
        # private testability seams:
        _now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        _pid: Callable[[], int] = os.getpid,
        _boot_id: Optional[str] = None,  # default = uuid.uuid4().hex[:8]
    ) -> None:
        # When enabled, ensure root exists: root.mkdir(parents=True, exist_ok=True).
        # Generate boot_id if not injected. Initialize handle/date as None.

    def cleanup_old_files(self) -> None:
        """Scan root for {payloads,raw_envelopes}-*.jsonl, delete files where
        age >= retention_days. Each unlink wrapped in try/except. Never raises.
        Called at Worker startup and on UTC date rollover."""

    def record(self, task: ClaimedTask) -> None:
        """Append one typed-stream JSON line. Never raises. No-op when disabled."""

    def record_raw(self, raw: dict, error: str) -> None:
        """Append one raw-envelope JSON line. Called from BackendClient.claim_next
        when ClaimedTask.from_dict() raises. Never raises. No-op when disabled."""

    def close(self) -> None:
        """Flush + close any open handles. Idempotent. Called from
        Worker.run_forever's finally block."""
```

State per stream: `{stream_name: (current_date, file_handle)}`. Both streams share `boot_id`, `degraded` flag, and the `_now` / `_pid` seams.

The file handle is held open across calls to avoid open/close overhead per task (microseconds matter on a tight polling loop). After each `write()`, the logger calls `flush()`. On UTC date rollover (checked at the start of every `record*` call against `_now().date()`), the old handle is closed before the new one opens, then `cleanup_old_files()` runs.

I/O is synchronous — one append per task, microseconds, on a polling loop that already does HTTP round-trips. Async wrapping would add complexity for no measurable gain.

JSON serialization uses `json.dumps(record, default=str)` so non-JSON-native values inside `params` (e.g., `Path`, `datetime`) don't crash `record()`. If even `default=str` raises, the failure is caught in the outer try/except per the failure-mode contract above.

### `Worker` integration (`src/task_worker_api/worker.py`)

1. `Worker.__init__` constructs a `PayloadLogger`:
   - `root`: `Path(shared_volume_path) / "_worker_payloads" / worker_id` if `shared_volume_path` is set, else a placeholder path with `enabled=False`.
   - `retention_days`: `int(os.environ.get("WORKER_PAYLOAD_LOG_RETENTION_DAYS", "14"))`.
   - `enabled`: `(shared_volume_path is not None) and os.environ.get("WORKER_PAYLOAD_LOG_ENABLED", "true").lower() != "false"`.
   - When the SDK constructs its own `BackendClient` (i.e., `client` arg not provided), it threads `payload_logger=self._payload_logger` into the `BackendClient` constructor. When the caller injects a custom client (e.g., `FakeBackendClient` in tests), the SDK doesn't override that wiring.

2. `Worker.run_forever`:
   - Logs one `INFO` line at startup describing payload-logging state ("enabled, root=…" or "disabled, reason=…").
   - Calls `self._payload_logger.cleanup_old_files()` once before entering the loop.
   - Wraps the polling loop in `try/finally`; the `finally` calls `self._payload_logger.close()`.

3. `Worker._run_one` calls `self._payload_logger.record(task)` as the **first statement** of the `try:` block — before `params_schema(**task.params)`, so malformed-payload tasks still produce a typed-stream record.

### `BackendClient` integration (`src/task_worker_api/client.py`)

`BackendClient.__init__` gains an optional `payload_logger: Optional[PayloadLogger] = None` keyword argument. `BackendClient.claim_next`:

1. Issues the HTTP request as today.
2. If the response indicates no claim (`task is None`), return None without logging anything.
3. Otherwise, attempt `ClaimedTask.from_dict(raw)`. On `Exception` from `from_dict`:
   - If `payload_logger` is set, call `payload_logger.record_raw(raw, error=str(exc))`.
   - Re-raise (the existing call-site catches and logs as before).
4. On success, return the `ClaimedTask`. The typed record is logged later by `Worker._run_one`.

The layering: `Worker` owns the `PayloadLogger` lifecycle; `BackendClient` is a passive consumer of an injected reference. No circular imports (`payload_log.py` imports `ClaimedTask` from `context.py`; `client.py` imports both).

### Public API

Neither `PayloadLogger` nor the new `BackendClient(payload_logger=...)` keyword is added to `__all__`. They're internal collaborators. Exposing them would invite consumers to instantiate them directly, fragmenting the contract.

### Version bump

`__version__` → `"0.5.0"`. CHANGELOG entry under a new `## [0.5.0]` heading.

## Tests (`tests/test_payload_log.py`)

Pure-unit tests against `PayloadLogger` (using `tmp_path` for the root):

- **Cold start mkdir.** Instantiating with a non-existent root creates `_worker_payloads/{worker_id}/`.
- **Disabled flag short-circuits.** `enabled=False` → no mkdir, no record, no cleanup, no boot_id allocation. Worker integration tests that don't pass `shared_volume_path` rely on this.
- **Round-trip typed.** `record(task)` writes a parseable JSON object containing `stream:"typed"` and the full envelope. Reading back yields the same fields.
- **Round-trip raw.** `record_raw(raw, error)` writes a parseable JSON object containing `stream:"raw"`, the original dict, and the error string.
- **Date rollover per-write.** Inject a `_now` that advances across UTC midnight between two `record()` calls. Assert: second record lands in the next day's file, previous handle closed, second day's file created.
- **Idle-across-midnight rollover.** Single `record()` call after `_now` has advanced multiple days. Assert: a file for the new date is created (not the original date).
- **Retention boundary.** Files at 7 / 14 / 30 days old (set via `os.utime`); `cleanup_old_files(retention_days=14)` deletes 14d and 30d, keeps 7d. (Inclusive `>=` on the boundary.)
- **Cleanup on rollover.** Drive `_now` across midnight; assert `cleanup_old_files` ran after the new file opened.
- **Cleanup race tolerance.** Two `PayloadLogger` instances on the same root; both call `cleanup_old_files()` against a pre-existing expired file; both return without raising. Use a stub `os.remove` that raises `FileNotFoundError` on the second call.
- **Filename uniqueness across processes.** Two `PayloadLogger`s with the same `worker_id`, different injected PIDs / boot_ids; each writes 100 records; assert directory contains two `payloads-*.jsonl` files, each with 100 valid JSON lines.
- **JSON serialization fallback.** `params` containing a `Path` and a `datetime` round-trips via `default=str` without raising.
- **Per-record size cap.** A `params` field of 1MB triggers truncation; record contains `{"_truncated": true, "_original_size_bytes": N}`; first occurrence emits a WARNING, second is silent.
- **Degraded mode.** Monkeypatch the file-open path to raise `OSError`. First `record()` call logs WARNING, subsequent silent; all calls return normally.
- **Close idempotent.** `close()` called twice does not raise.
- **8-char boot_id.** Constructed without `_boot_id` injection, the boot_id is 8 hex chars (uuid4-derived).

Integration test additions to `tests/test_worker_loop.py`:

- **Worker writes typed record on happy path.** Use a `tmp_path` shared_volume_path; assert one line in `payloads-*-pid*-*.jsonl` after `run_one()`.
- **Worker writes typed record even when params validation rejects.** Existing schema-rejection test; add the assertion.
- **No shared_volume_path → logger disabled, no files.** Existing tests already pass `shared_volume_path=None`; this test makes it explicit.
- **Raw envelope captured on protocol drift.** Inject a `FakeBackendClient` that returns a response dict with an unknown `task_type`; assert `raw_envelopes-*.jsonl` contains the bad envelope.

## Documentation

`docs/adding-a-worker.md` gains a new "Replaying captured payloads" subsection (~10 lines) showing how to read a `.jsonl` file and re-enqueue a task locally for debugging. Without this, the feature is technically complete but operators have to invent the workflow themselves.

## Deployment-side changes (`syngar-deployment-scripts/surgiclaw`)

Tracked here because the feature is unusable without them. Ships as a separate PR.

### `.env.example`

Add a new `# ----- Worker payload logging -----` block, near the worker block (~line 100):

```bash
# Worker payload logging — captures every claimed task's full envelope to
# /app/shared/_worker_payloads/{worker_id}/payloads-YYYY-MM-DD-pidNNN-XXXX.jsonl
# inside each worker container. Used to reproduce worker bugs and replay
# real producer traffic into tests. Files older than the retention window
# are deleted on worker startup and on UTC date rollover.
WORKER_PAYLOAD_LOG_ENABLED=true
WORKER_PAYLOAD_LOG_RETENTION_DAYS=14
```

### `.env`, `.env.linux`

Add the same two keys with the same defaults. Operator's local `.env` may override per deployment; `.env.linux` mirrors `.env.example` so a fresh Linux clone works without manual edits.

### `docker-compose.yml`

Add to each worker service's `environment:` block (`neural-canvas`, `blender-worker`, `colmap-splat-worker`):

```yaml
- WORKER_PAYLOAD_LOG_ENABLED=${WORKER_PAYLOAD_LOG_ENABLED:-true}
- WORKER_PAYLOAD_LOG_RETENTION_DAYS=${WORKER_PAYLOAD_LOG_RETENTION_DAYS:-14}
```

No volume changes — the existing `${SHARED_DATA_PATH}:/app/shared` mount is what the SDK writes into.

### `docker-compose.linux.yml`

No changes. The override file currently only patches `nexus-core`; worker services inherit `environment:` from the base compose unchanged.

## Failure modes (per-codepath)

| Codepath | Realistic failure | Test? | Error handling? | User-visible? |
|---|---|---|---|---|
| `PayloadLogger.__init__` (mkdir) | Permission denied on `_worker_payloads/{worker_id}/` | yes (degraded test) | yes (caught, INFO log, logger disabled) | clear startup log |
| `PayloadLogger.record` (write) | Disk full, fs flap | yes (degraded test) | yes (one WARNING, then silent) | one log line |
| `PayloadLogger.record` (json.dumps) | Non-serializable param | yes (default=str test) | yes (default=str fallback; outer try/except for residue) | none on happy path |
| `PayloadLogger.record` (oversized) | 1MB+ params blob | yes (size cap test) | yes (truncation marker) | one WARNING |
| `PayloadLogger.cleanup_old_files` (race) | Two replicas unlink same file | yes (race tolerance test) | yes (per-file try/except) | none |
| `BackendClient.claim_next` (from_dict) | Backend ships new task_type before workers update | yes (raw-envelope test) | yes (raw record + re-raise) | raw envelope captured |
| `Worker.run_forever` (logger.close) | Handle already closed | yes (close idempotent test) | yes (idempotent) | none |

No critical gaps — every failure mode has a test, error handling, and either a user-visible log line or a clearly-intended silent recovery.

## Worktree parallelization strategy

Sequential implementation. The SDK changes (`payload_log.py` + `worker.py` + `client.py` + tests) all touch overlapping module imports and share the new `PayloadLogger` type. No meaningful parallelization win.

The deployment-side changes (`syngar-deployment-scripts/surgiclaw`) are independent of the SDK and can be drafted in parallel — but they can't ship until the SDK release is published, so the parallelism is bounded.

## Order of work

1. **SDK PR.** `payload_log.py` + `Worker` integration + `BackendClient` integration + tests + `docs/adding-a-worker.md` replay subsection + CHANGELOG + version bump to v0.5.0. Merge and publish wheel via existing GitHub Releases workflow.
2. **Worker repos** (`blender-worker`, `colmap-splat-worker`, `neural-canvas`): bump `task-worker-api` dependency to `>=0.5.0`. No code changes — they consume the SDK transparently.
3. **Deployment PR** (`syngar-deployment-scripts/surgiclaw`): `.env`, `.env.linux`, `.env.example`, `docker-compose.yml`. Pull updated worker images. Deploy.

Steps 2 and 3 can land in either order; the SDK gates the feature behind the env var, so deploying the compose change before the worker images upgrade is a no-op rather than a regression.

## Cross-model tension (review vs outside voice)

Outside voice argued the worker-side capture is solving a server-observability problem at the wrong layer — the backend already stores `task.params` and a `GET /tasks/{id}/envelope` endpoint would obviate this whole feature. The review accepted the worker-side approach because the user explicitly requested in-container capture. **Captured as a TODO** for SynPusher-Vue's Nexus Core: add a backend admin endpoint that returns the stored envelope for a given task_id, as a complementary debugging surface (especially useful for tasks the worker never claimed or for backend-only reproduction).
