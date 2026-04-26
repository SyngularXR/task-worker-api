# Changelog

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

## v0.4.1 — 2026-04-24

- `upload_outputs` now stages local-mode outputs under
  `shared_volume_path/temp/{task_id}/` instead of
  `shared_volume_path/{task_id}/`. Keeps the shared volume root clean
  and gives the backend mirror an obvious place to `rmdir` once it has
  moved the artifacts to their permanent home. Behaviour is otherwise
  unchanged — the return value is still a `{key: absolute_path}` map
  pointing at whatever location the SDK chose.

## v0.3.1 — 2026-04-22

- Python floor lowered to 3.10 (was 3.11). Neural-Canvas runs 3.10
  and needed the SDK to consume there.
- `run_hybrid` rewritten to use `asyncio.wait` + explicit cancel
  instead of `asyncio.TaskGroup` (3.11+ only). Same semantics: if
  either the FastAPI app or Worker exits, the other gets cancelled
  cleanly and the first exception propagates to the caller.
- No public API change otherwise.

## v0.3.0 — 2026-04-22

- Adds `GsBuildParams` schema (colmap-splat worker) with all 11 run.sh
  knobs (`scene`, `iterations`, `max_splats`, `sh_degree`, `seed`,
  `num_threads`, `background`, `strategy`, etc.). All fields optional
  except one of `scene` / `scene_path`.
- Adds `SegmentationParams` schema (Neural-Canvas worker) with
  `input_path`, `model`, `labels`, `case_id`, `dicom_id`, `mask_id`.
- Registers both in `TASK_PARAMS_SCHEMAS`.
- TypeScript codegen picks them up automatically; regenerated
  `artifacts/task-worker-types/index.ts` ships in this release.

`RenderParams` and `AppleMlGsParams` still deferred — audit pending.

## v0.2.0 — 2026-04-22

Adds the runtime SDK — workers can now depend on this package and
reduce `main.py` to ~20 lines.

**New modules:**
- `client.py` — `BackendClient` async HTTP wrapper with retry-on-
  transient-transport-error. Claim / progress / complete / fail /
  cancel-status / file transfer.
- `context.py` — `ClaimedTask` (typed task row), `FileContext`,
  `TaskContext` (what handlers receive).
- `files.py` — `prepare_inputs` / `upload_outputs` with local
  (shared volume) vs remote (HTTP transfer) auto-detection based
  on `task.params` keys.
- `cancel.py` — `CancelGuard` async context manager. Three
  documented patterns: pure async, subprocess (Blender, colmap),
  threadpool (Neural-Canvas GPU). `on_cancel` hook lets handlers
  provide a termination handle.
- `progress.py` — `ProgressReporter` with a background heartbeat
  loop. `update()` for stage transitions; `raise_if_cancelled()`
  for handlers that want to bail between blocking ops.
- `worker.py` — `Worker.run_forever()`. Does claim + validate +
  stage-inputs + heartbeat + cancel-guard + publish-outputs + error
  handling + polling. Handlers implement just
  `async def run(ctx, params) -> dict`.
- `worker.run_hybrid(app_coro, worker)` — helper for running the
  Worker alongside an existing event loop (e.g. Neural-Canvas's
  uvicorn.Server).
- `testing.py` — `FakeBackendClient` drop-in for tests.

**Dependency change:**
- `httpx>=0.23` added (was intentionally omitted from v0.1.0 to
  avoid forcing an upgrade on the SynPusher backend's pinned 0.23.3).
  The BackendClient uses only the stable AsyncClient surface that
  hasn't changed across 0.23–0.28.

**Tests:**
- `tests/test_worker_loop.py` — 6 tests covering the happy path,
  `extra="forbid"` rejection, handler exceptions, cooperative
  cancel, no-handler-registered path, and the schema registry.

## v0.1.0 — 2026-04-22

Initial scaffold (schemas + enums + errors). See git tag v0.1.0.
