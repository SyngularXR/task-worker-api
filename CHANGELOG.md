# Changelog

## Unreleased

**Fixes:**
- `BackendClient` retry backoff is now capped and jittered. The exponential
  schedule (`retry_backoff_s * 2**n`) grew without bound: a supported
  `max_retries=8` with the default `retry_backoff_s=2.0` would sleep 256s on
  the penultimate attempt, blocking the worker's event loop for ~10 minutes on
  a single `claim_next` / `complete` call. A new `retry_backoff_max_s`
  parameter (default `60.0`) clamps each inter-attempt delay; pass `None` to
  recover the legacy unbounded behaviour. A new `retry_jitter` parameter
  (default `True`) applies ±25% random jitter to each delay, decorrelating
  retries across the fleet — Neural-Canvas, Blender-CLI, and colmap-splat all
  poll the same backend, so without jitter they retry in lockstep and
  re-overload it the instant it recovers (thundering herd). Both parameters
  are additive and backward-compatible; existing consumers see no behavioural
  change at the default `max_retries=4` (delays 2/4/8s stay well under the cap,
  and jitter only spreads them within ±25%).
- `BackendClient` now retries transient 5xx gateway status codes (502/503/504)
  with the same exponential backoff as transport errors. The backend sits
  behind nginx; a 502/503/504 almost always means the Flask upstream restarted,
  was momentarily overloaded, or the gateway timed out — a blip that clears in
  seconds. Previously every `HTTPStatusError` (including these transient
  gateway codes) surfaced immediately, failing the task on a momentary outage
  that a single retry would have absorbed. 500 (the application's own error —
  usually a logic bug or bad payload) and 4xx (client error) still surface
  immediately without consuming retry budget, matching the existing
  non-transient pass-through contract. The fix required moving
  `raise_for_status()` *inside* the retry closure (it was previously called
  after `_request` returned, so status errors never reached the retry loop);
  `claim_next` now calls `_retry` directly with its own closure so it can still
  treat 204/404 as success variants before any status check.

- `BackendClient.__init__` now rejects `max_retries < 1` with a clear
  `ValueError` at construction. Previously, `max_retries=0` made the retry
  loop in `_retry` execute zero iterations, leaving `last_exc` as `None` and
  tripping a bare `assert last_exc is not None` — an opaque `AssertionError`
  that crashed the worker process. `max_retries` is the total number of
  attempts (not retries on top of one), so `< 1` is degenerate and should
  never reach the retry loop. The `_retry` method's post-loop `assert` was
  also replaced with an explicit `RuntimeError` guard (a bare `assert` is
  stripped under `python -O`, which would turn the crash into a silent
  `None`-return). Every current consumer uses the default `max_retries=4`,
  so the validation only fires on new misconfigurations.
- `Worker.__init__` now validates that an externally-supplied `client`'s
  `base_url` is compatible with the worker's `backend_url` (equal, or one is
  a prefix of the other, ignoring trailing slashes). A mismatch silently
  routed every request — claim, complete, file transfer — to the wrong
  endpoint: tasks were claimed-but-never-completed or completed against the
  wrong tenant, with no error until an operator noticed the missing activity.
  The guard logs a warning and raises `ProtocolError` at construction time,
  surfacing the misconfiguration at boot. The check is skipped for clients
  without a `base_url` attribute (e.g. `FakeBackendClient`), which make no
  real HTTP calls, so the test double remains a true drop-in. Every current
  consumer that passes `client=` passes one with a matching base URL, so the
  validation only fires on new misconfigurations.

- `BackendClient.download_file` now removes any partial file left at `dest`
  when the download fails (retries exhausted or a non-retryable HTTP error).
  Previously, a mid-stream transport failure could leave a truncated file on
  disk — each retry truncated via `"wb"` so a *successful* retry was clean,
  but if all retries failed the last partial content survived at `dest`,
  silently corrupting downstream consumers that checked `dest.exists()`.
  The cleanup uses `dest.unlink()` in a `try/except` so a missing or
  already-removed file is a no-op. The existing contract documented in
  `test_download_file_raises_after_exhausting_retries` ("dest must not be
  left as a partial file") now holds for the real-world mid-stream failure
  case, not only for stream-establishment failures.

- `BackendClient.upload_file` now retries on transient transport errors
  (`httpx.TransportError` / `httpx.TimeoutException`) with the same
  exponential backoff as every other backend call. Previously the source
  file was opened **once outside** the retry loop; httpx consumed the
  handle to EOF on the first attempt's request construction, so every
  retry silently sent zero bytes — a data-corruption bug that produced
  empty/truncated uploads after a transient network hiccup. The fix moves
  `open()` inside a per-attempt `_upload_once` closure (mirroring
  `download_file`'s `_stream_once` pattern), so each retry gets a fresh
  file handle starting at byte 0. Non-transient HTTP status errors
  (404/500) still surface immediately without consuming retry budget.

- `BackendClient.download_file` now retries on transient transport errors
  (`httpx.TransportError` / `httpx.TimeoutException`) with the same
  exponential backoff as every other backend call. It was the only API
  method that streamed directly via `httpx.AsyncClient.stream`, bypassing
  the retry loop in `_request`, so a network hiccup during file transfer
  failed the task outright instead of recovering. Each retry attempt
  re-opens the destination with `"wb"` (truncating), so a mid-stream
  failure followed by a successful retry produces a correct file. The
  shared backoff loop was extracted into `_retry` (used by both `_request`
  and `download_file`); non-transient HTTP status errors (404/500) still
  surface immediately without consuming retry budget.

**New:**
- Test coverage for `BackendClient._request` retry/backoff logic and the real
  (non-Fake) `download_file`/`upload_file` HTTP paths (`tests/test_client_retry.py`).
  The retry-on-transient-error contract — retry count, exponential backoff
  scheduling, exhaustion re-raise, and non-retryable-error pass-through — was
  previously untested. Also adds the first coverage of `run_hybrid`
  (`tests/test_run_hybrid.py`): concurrent app+worker lifecycle, clean
  cancellation when either side exits, and exception propagation semantics.
  Now extended to cover the `download_file` retry path (transport error
  recovery, timeout recovery, retry exhaustion, non-transient pass-through,
  backoff scheduling, and clean-file-after-midstream-retry) and the
  partial-file cleanup on failure (stale/partial `dest` removal on both
  retry exhaustion and non-transient errors, plus a guard that successful
  downloads are unaffected).

- `FakeBackendClient` now supports in-memory remote-mode file transfer.
  `download_file` / `upload_file` no longer raise `NotImplementedError`:
  stage virtual inputs with `queue_file(task_id, filename, content)` and
  assert on the public `uploaded_files` dict (keyed by
  `(task_id, filename) -> bytes`). Lets worker test suites (colmap-splat,
  Neural-Canvas, Blender-CLI) exercise the `input_files` remote path
  without `httpx.MockTransport`. Downloads of unstaged files raise
  `FileNotFoundError`, mirroring a backend 404.

**Fixes:**
- Register `TaskType.GS4D_BUILD` in `TASK_PARAMS_SCHEMAS` (via the
  `Gs4dBuildParams = GsBuildParams` alias). The enum member was added in
  v0.10.0 but never registered, so `Worker.__init__` rejected any handler for
  it — blocking the colmap-splat worker's 4D warm-chain. The alias shares one
  Pydantic model so the 4D per-phase training tasks reuse the identical params
  surface (`scene`, `warm_start_ply`, tuning knobs).
- Fix duplicate-interface bug in `tools/gen_typescript.py`: when two registry
  entries alias the same Pydantic model, `model_cls.__name__` is identical for
  both, so the codegen emitted `export interface GsBuildParams` twice — a TS
  compilation failure in upstream consumers. Each named interface is now
  emitted exactly once; both keys still appear in `TaskParamsByType`.
- Regenerate `artifacts/task-worker-types/index.ts` (was stale since v0.10.0 —
  missing `gs4d_build` / `finalize_segment` from the TaskType enum).

## v0.10.0 — 2026-06-29

4D Gaussian-splatting (cardiac) support for the task queue.

**New:**
- `TaskType.GS4D_BUILD` (`"gs4d_build"`) — 4D Gaussian-splatting build over N
  cardiac phases (warm-start chain). Value ≤20 chars for the backend's
  `task_type` column.
- `GsBuildParams.warm_start_ply` (optional) — the prior phase's trained PLY to
  seed this phase's Gaussian init from (run.sh `--warm-start-ply`). Lets the 4D
  warm-chain reuse the existing `gs_build` worker; `None` = cold start.

## v0.7.0 — 2026-06-05

Per-task execution timeout. `Worker` now enforces a wall-clock deadline per
task via an OS watchdog thread that fires even when a handler blocks the event
loop. On expiry it SIGTERM→SIGKILLs the task-spawned child processes and fails
the task terminally (`timeout: exceeded Ns`); an in-process wedge with nothing
to kill triggers a bounded synchronous fail + process exit (the container
`restart: unless-stopped` recovers). Motivated by a production blender wedge
(a runaway convex-hull held a worker for 3.5h, offline >1h).

**New:**
- `Worker(task_timeout_s=1800.0, task_timeouts={TaskType: seconds},
  timeout_grace_s=15.0, on_hard_exit=...)` plus env
  `WORKER_TASK_TIMEOUTS="default=1800,gs_build=7200"`. Resolution order:
  env per-type → ctor per-type → env default → ctor default. A resolved
  value `<= 0` disables the timeout for that task type.
- `task_worker_api.watchdog` (`TaskWatchdog`, `TerminalGuard`,
  `list_descendants`, `kill_procs`) and `task_worker_api.timeouts`
  (`resolve_task_timeout`, `parse_timeouts_env`).

**Notes:**
- Known limitations (see the design spec): a pure-Python GIL-holding busy loop
  can starve the watchdog thread; child attribution in `run_hybrid` workers is
  snapshot-delta based. Process-per-task is the documented future hardening.

## v0.6.1 — 2026-05-11

Adds an optional `dense_init` field to `GsBuildParams` for the
colmap-splat worker. Without this, producers stamping `dense_init` into
`task.params` would trip `extra="forbid"` validation on claim and
silently fail every GS_BUILD task.

**New:**
- `GsBuildParams.dense_init: Optional[bool]` — when true, the worker
  runs COLMAP dense MVS (`image_undistorter` → `patch_match_stereo` →
  `stereo_fusion`) after the sparse mapper and seeds splat init from
  the fused point cloud.

## v0.6.0 — 2026-04-28

Adds `DEPLOY_CASE` task type for the assetbundle-builder worker.

**New:**
- `TaskType.DEPLOY_CASE = "deploy_case"` in `enums.py`.
- `DeployCaseParams` Pydantic v2 schema: required `content_path` (absolute
  path to case content folder on shared volume) and optional `build_target`
  (default `"Android"`, passed to Unity CLI `-buildTarget` flag).
- `TASK_PARAMS_SCHEMAS[TaskType.DEPLOY_CASE]` registered.
- 5 new schema tests cover registration, roundtrip, default, extra-field
  rejection, and missing required field.

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
