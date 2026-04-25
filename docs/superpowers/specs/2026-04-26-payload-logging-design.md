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

**Env-var parsing.** Worker startup parses `int(WORKER_PAYLOAD_LOG_RETENTION_DAYS)`; on `ValueError` (e.g., `"abc"`) or non-positive result, fall back to `14` and emit a `WARNING`. Setting `0` would otherwise delete everything immediately on first cleanup, which is a footgun.

Cleanup runs:
1. **At `Worker.run_forever` startup**, before the polling loop.
2. **On UTC date rollover**, immediately after the new day's file is opened.
3. **On a periodic timer** inside `run_forever` (default every hour). Without (3), an idle worker that wrote files 30 days ago and then sat dormant would never clean up — rollover-triggered cleanup only fires when the next write happens. The timer is cancelled in `finally` alongside the heartbeat plumbing.
4. **Even when `enabled=False`.** A deployment that flips the kill switch shouldn't leave residual logs sitting on disk indefinitely. Cleanup is the one operation that runs regardless of `enabled`; `record()` and `record_raw()` remain no-ops.

Cleanup scans `_worker_payloads/{worker_id}/{payloads,raw_envelopes}-*.jsonl` and removes files where `now - mtime >= retention_days`. Boundary is inclusive on `==`. Each `os.remove` is wrapped in `try/except (FileNotFoundError, PermissionError, OSError)`: cleanup races between replicas, files held open by another process on Windows, or transient fs flaps all degrade to "skip this file, continue with the rest." Cleanup never raises out of the logger.

`mtime` (not the date in the filename) is the deletion invariant. Filesystem `mtime` can be perturbed by `cp -p`/restores/host tools; this is acceptable for the worker's volume because we don't restore-from-backup into it. If that assumption ever changes, switch to filename-date parsing.

## Per-record size cap

Cap each record at **256KB** (UTF-8 encoded JSON length). Two-stage check, in this order:

1. **Pre-serialization payload check.** Serialize only the variable-size field once: `serialized = json.dumps(params, default=str)` (or `raw` for raw-envelope records). If `len(serialized.encode("utf-8")) > PAYLOAD_CAP_BYTES` (224KB, leaves headroom for envelope fields), replace the field with `{"_truncated": true, "_original_size_bytes": N}` before constructing the wrapper record. This bounds CPU/memory: a 50MB params blob is serialized exactly once, then dropped.
2. **Post-construction final check.** After building the full record dict and serializing it, if the result still exceeds 256KB (e.g., because `worker_id`, `error`, or `item_key` are themselves pathological), replace the entire record body with `{"_record_truncated": true, "_original_size_bytes": N, "task_id": ..., "captured_at": ...}` so we keep enough metadata to know something happened.

On the **first** truncation per process lifetime (either stage), log one `WARNING` naming the task_id, stage, and original size. Subsequent truncations are silent regardless of stage.

The cap protects against "50MB params dict × hundreds of tasks/day × 14 days = unbounded disk." Realistic params are <10KB; truncation is exception, not norm.

## Default behaviour and opt-out

On by default. Disable per deployment with `WORKER_PAYLOAD_LOG_ENABLED=false`.

The whole point is "have the evidence when something breaks" — opt-in defeats that. Disk cost is bounded by retention + per-record cap, and the file lives inside the same trust boundary as `case_data`. The medical (surgiclaw) deployment can opt out via `.env` if compliance disagrees; SDK ships sensible defaults, deployment-side overrides handle policy.

## Failure mode

`PayloadLogger.__init__`, `.record()`, `.record_raw()`, `.cleanup_old_files()`, and `.close()` **must never raise**. The contract covers `__init__` too: a `PermissionError` on `mkdir`, an invalid `worker_id`, or any other startup failure makes the logger self-disable rather than crash `Worker.__init__`.

On any failure inside one of these methods:

1. Log a single `WARNING` (uniform level — no INFO/WARNING split) via the `task_worker_api.payload_log` logger, including the underlying exception type and message.
2. Set a `_warned_once` flag to suppress *repeated WARNING log lines*. The logger keeps trying on subsequent calls — transient failures (fs flap, brief permission glitch) recover on their own, and a permanent failure just produces silent no-ops after the first warning. Disabling capture for the entire process lifetime on one transient OSError is too aggressive.
3. Return without raising. The worker keeps polling and running tasks.

Durability: `flush()` is called after every write. A Python-process crash loses at most the in-flight task. A kernel panic, Docker Desktop VM crash, or host power loss can lose more — `fsync` is **not** called per record because the latency cost outweighs the rarity of the loss class, especially on Windows Docker Desktop where the WSL2 9P bind mount makes `flush()` itself meaningfully more expensive than on native Linux. Payload logging is a debug aid, not a correctness feature.

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

    def record_raw(self, raw: Any, *, error_type: str, error: str) -> None:
        """Append one raw-envelope JSON line. Called from BackendClient.claim_next
        when ClaimedTask.from_dict() raises OR when the response itself fails to
        parse as JSON / yields an unexpected shape (None, list, etc.). `raw` may
        be any JSON-coercible value or unparseable text. `error_type` is the
        exception class name (e.g. 'ValueError', 'KeyError'); `error` is the
        message. Never raises. No-op when disabled."""

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
   - `root`: `Path(shared_volume_path) / "_worker_payloads" / sanitized_worker_id` if `shared_volume_path` is set, else a placeholder path with `enabled=False`. **Sanitization rule:** `re.sub(r"[^A-Za-z0-9._-]", "_", worker_id)`, plus reject empty/`.`/`..`/Windows reserved names (`CON`, `PRN`, `AUX`, `NUL`, `COM1`-`COM9`, `LPT1`-`LPT9`) by appending `_x` if matched. This protects against path traversal, slashes/backslashes, colons, and reserved Windows device names that would crash the mkdir on Windows Docker Desktop.
   - `retention_days`: parsed via `int()` from `WORKER_PAYLOAD_LOG_RETENTION_DAYS` with a try/except fallback to `14` and a `WARNING` on parse failure or non-positive value.
   - `enabled`: `(shared_volume_path is not None) and os.environ.get("WORKER_PAYLOAD_LOG_ENABLED", "true").lower() != "false"`.
   - When the SDK constructs its own `BackendClient` (i.e., `client` arg not provided), it threads `payload_logger=self._payload_logger` into the `BackendClient` constructor. When the caller injects a custom client (e.g., `FakeBackendClient` in tests), the SDK doesn't override that wiring — so injection-style tests must pass a logger explicitly if they want to exercise the raw-envelope path.

2. `Worker.run_forever`:
   - Logs one `INFO` line at startup describing payload-logging state ("enabled, root=…" or "disabled, reason=…").
   - Calls `self._payload_logger.cleanup_old_files()` once before entering the loop.
   - Schedules a periodic cleanup task (default every hour, configurable via `WORKER_PAYLOAD_LOG_CLEANUP_INTERVAL_S=3600`) on the same event loop. The task is cancelled in the existing `finally` block alongside heartbeat shutdown.
   - Wraps the polling loop in `try/finally`; the `finally` calls `self._payload_logger.close()`.

3. `Worker._run_one` calls `self._payload_logger.record(task)` as the **first statement** of the `try:` block — before `params_schema(**task.params)`, so malformed-payload tasks still produce a typed-stream record.

### `BackendClient` integration (`src/task_worker_api/client.py`)

`BackendClient.__init__` gains an optional `payload_logger: Optional[PayloadLogger] = None` keyword argument. `BackendClient.claim_next`:

1. Issues the HTTP request as today.
2. **Capture the JSON-decoded body before any structural assumptions.** Wrap `response.json()` in try/except. If it raises (`json.JSONDecodeError`), call `payload_logger.record_raw(raw=response.text, error_type='JSONDecodeError', error=str(exc))` and re-raise.
3. If the body is `None` or otherwise indicates no claim, return None without logging.
4. Otherwise, attempt `ClaimedTask.from_dict(body)`. On any `Exception` from `from_dict` (including unexpected shape — `body` being a list, missing keys, bad enum values):
   - Call `payload_logger.record_raw(raw=body, error_type=type(exc).__name__, error=str(exc))`.
   - Re-raise (the existing `Worker._claim` call-site catches and logs as before).
5. On success, return the `ClaimedTask`. The typed record is logged later by `Worker._run_one`.

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

Integration test additions to `tests/test_worker_loop.py` (using the existing `FakeBackendClient`-based fixture):

- **Worker writes typed record on happy path.** Use a `tmp_path` shared_volume_path; assert one line in `payloads-*-pid*-*.jsonl` after `run_one()`.
- **Worker writes typed record even when params validation rejects.** Existing schema-rejection test; add the assertion.
- **No shared_volume_path → logger disabled, no files.** Existing tests already pass `shared_volume_path=None`; this test makes it explicit.
- **`worker_id` sanitization.** Construct a `Worker` with `worker_id="../etc/passwd"` and `worker_id="CON"`; assert the resulting directory is sanitized and lives under `_worker_payloads/`.
- **Bad `WORKER_PAYLOAD_LOG_RETENTION_DAYS` falls back.** Monkeypatch env to `"abc"` then `"0"` then `"-5"`; assert logger uses `14` and emits a WARNING.

New tests in `tests/test_payload_log_integration.py` — covering the paths `FakeBackendClient` cannot exercise:

- **`BackendClient` raw-envelope on `from_dict` failure.** Use `pytest-httpx` (or `httpx.MockTransport`) to make `BackendClient.claim_next` return a real HTTP response with an unknown `task_type` int; assert the *real* `BackendClient` (not `FakeBackendClient`) calls `payload_logger.record_raw` and re-raises. This test exists specifically because `FakeBackendClient` would bypass the protocol-drift codepath under test.
- **`BackendClient` raw-envelope on JSON parse failure.** Mock transport returns invalid JSON body; assert raw-envelope record contains the response text plus `error_type="JSONDecodeError"`.
- **Startup path runs cleanup + INFO log.** Drive `Worker.run_forever()` with a stop event set after one tick; assert the INFO log line was emitted and `cleanup_old_files()` was called. `Worker.run_one()` does NOT exercise this path, so the existing fast-path tests cannot substitute.
- **Shutdown path closes logger.** Same harness; assert `logger.close()` was called in `finally` and is idempotent if called again.
- **Periodic cleanup timer fires.** Set `WORKER_PAYLOAD_LOG_CLEANUP_INTERVAL_S=0.1`, run for 0.5s, assert cleanup ran multiple times.

Pure-unit additions:

- **Final-record-size cap.** Construct a record with a small `params` but a 1MB `error` string in the raw stream; assert the record body is replaced with the `_record_truncated` marker.
- **Pre-serialization cap measures once.** Use a custom `params` object whose `__repr__` increments a counter; assert it was serialized exactly once even when truncation triggers.
- **Cleanup on Windows-style PermissionError.** Stub `os.remove` to raise `PermissionError` on a target file; assert other expired files are still removed and no exception escapes.
- **Cleanup runs even when `enabled=False`.** Construct disabled logger with pre-existing expired files; call `cleanup_old_files()`; assert files removed.
- **Degraded mode keeps retrying.** Stub `open()` to raise `OSError` on the first call only; first `record()` logs WARNING, second `record()` succeeds without warning. (Validates that we suppress repeat warnings, not retries.)

## Documentation

`docs/adding-a-worker.md` gains a new "Replaying captured payloads" subsection. It must spell out the **replay transform** explicitly — a typed log line carries fields an operator should NOT pass back to the backend on re-enqueue:

```
typed-stream JSONL fields:
  captured_at     -- DROP (replay metadata, not part of original task)
  stream          -- DROP (always "typed")
  task_id         -- DROP (will be assigned a new id by backend on re-enqueue)
  status          -- DROP (will be PENDING after re-enqueue)
  worker_id       -- DROP (claim metadata, not part of the task spec)
  process_id      -- DROP
  boot_id         -- DROP
  task_type       -- KEEP (required for re-enqueue)
  case_id         -- KEEP
  item_key        -- KEEP
  params          -- KEEP
```

The subsection includes a 10-15 line Python snippet showing the transform and a `BackendClient.enqueue` call with the kept fields. Without this, an operator who copies the JSON line wholesale into a re-enqueue request will hit schema rejections and conclude the feature is broken.

## Platform notes

The `${SHARED_DATA_PATH}:/app/shared` bind mount is present on both Linux Docker (native) and Windows Docker Desktop (WSL2 9P), but the two are not behavior-identical:

- **Timestamp precision:** WSL2 9P rounds `mtime` to coarser granularity than ext4. Mtime-based retention may keep files a few seconds longer than expected on Windows. Acceptable.
- **Delete semantics:** Files held open by another process raise `PermissionError` on Windows and (typically) succeed on Linux. Cleanup catches both.
- **Sync write cost:** `flush()` and the kernel's eventual write-back can be meaningfully slower on 9P. Per-task overhead remains under 10ms in practice but is not "microseconds" as the simpler async-IO claim might imply. We accept this; payload logging is off the latency-critical path for task processing.
- **Path length:** Windows host paths can hit MAX_PATH limits when the mounted volume is deep. Surgiclaw's `${SHARED_DATA_PATH}` is operator-configurable and recommended to live under a short path (e.g., `D:\syngar\shared\`).

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
| `PayloadLogger.__init__` (mkdir) | Permission denied on `_worker_payloads/{worker_id}/` | yes (degraded test) | yes (caught, WARNING log, logger self-disables) | clear startup log |
| `PayloadLogger.__init__` (bad worker_id) | `..`, slash, Windows reserved name in worker_id | yes (sanitization test) | yes (sanitized at construction) | none |
| `Worker.__init__` (bad retention env) | `WORKER_PAYLOAD_LOG_RETENTION_DAYS=abc` or `0` | yes (env-fallback test) | yes (fallback to 14, WARNING) | clear startup log |
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
2. **Worker-repo audit** (prerequisite for step 3 to be a no-op). Each worker repo must already pass `shared_volume_path` into its `Worker(...)` constructor. The default value is `None`, which silently disables the logger — meaning a worker repo that *thinks* it's getting the feature might not be. Audit checklist for each of `blender-worker`, `colmap-splat-worker`, `neural-canvas` (`src/sdk_worker.py` or equivalent):
   - Reads `SHARED_VOLUME_PATH` (or `SHARED_DATA_PATH`) from env.
   - Passes it to `Worker(shared_volume_path=...)`.
   - If any repo doesn't, add the wiring there (small follow-up PR per repo). Bump `task-worker-api` dependency to `>=0.5.0` in the same PR.
3. **Deployment PR** (`syngar-deployment-scripts/surgiclaw`): `.env`, `.env.linux`, `.env.example`, `docker-compose.yml`. Pull updated worker images. Deploy.

Step 3 can land before step 2 finishes; the SDK gates the feature behind the env var, so deploying the compose change before the worker images upgrade is a no-op rather than a regression. But the audit in step 2 is what actually makes the feature appear in production logs — without it, the env var is a knob attached to nothing.

## Cross-model tension (review vs outside voices)

**Pass 1 (Claude subagent)** argued the worker-side capture is solving a server-observability problem at the wrong layer — the backend already stores `task.params` and a `GET /tasks/{id}/envelope` endpoint would obviate this whole feature. The review accepted the worker-side approach because the user explicitly requested in-container capture. **Captured as a TODO** for SynPusher-Vue's Nexus Core: add a backend admin endpoint that returns the stored envelope for a given task_id, as a complementary debugging surface (especially useful for tasks the worker never claimed or for backend-only reproduction).

**Pass 2 (Codex gpt-5.5)** surfaced 20 finer-grained findings, 18 of which were folded into this revision (env-var validation with safe fallback, `worker_id` path sanitization, `__init__` covered by never-raise contract, periodic cleanup timer for idle workers, two-stage size cap with pre-serialization measurement, broadened `record_raw` signature for non-dict raw values + JSON parse failures, `error_type` captured separately from `error` message, integration tests against real `BackendClient` not just `FakeBackendClient`, startup-path tests using `run_forever` instead of `run_one`, `worker_id` sanitization tests, retention env-fallback tests, cleanup tolerates `PermissionError` not just `FileNotFoundError`, cleanup runs even when `enabled=False`, degraded mode keeps retrying with suppressed warnings instead of disabling for life, replay-transform documentation, worker-repo audit step prerequisite to deployment, Platform notes section for Windows 9P caveats, mtime-vs-filename-date retention rationale). The two findings deferred (sync cleanup blocking event loop on rollover; per-record `flush()` cost on 9P) are noted in Platform notes but not addressed structurally — both are bounded enough that a benchmark-driven follow-up is the right next step if real volume warrants.
