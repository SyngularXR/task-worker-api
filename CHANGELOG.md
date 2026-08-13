# Changelog

## Unreleased

**Fixes:**
- `BackendClient.upload_file` now accepts an optional keyword-only
  `cancelled` event: it is checked before the PUT goes out, and the in-flight
  request is raced against it so a cancel arriving mid-upload aborts the
  request and raises `TaskCancelled` instead of streaming the rest of the
  body. `upload_outputs` passes the `CancelGuard`'s event through, so a
  cancel is now visible *during* a remote output upload instead of only
  between batch files. Previously the between-files check was the only cancel
  point in the remote publish path: a single-file output set — a lone
  colmap-splat PLY, a Neural-Canvas splat — has no such boundary, so a user
  cancel that arrived mid-upload streamed the whole multi-GB file to the
  backend before the worker noticed, burning minutes of upload bandwidth on a
  task nobody wanted. This is the upload-side counterpart of the
  `download_file` fix below. `TaskCancelled` is not a transient error, so it
  leaves the retry loop immediately without consuming retry budget (a retried
  cancel would re-send the same file). A request that has already completed
  wins the race, so a cancel arriving after delivery is not reported as an
  aborted upload. Both the losing request and the event waiter are awaited to
  completion before the race helper returns or raises — cancellation is only a
  *request*, so a helper that returned straight after `cancel()` would leave
  the PUT still reading `src` after `upload_file` closed it and tearing its
  connection down after the client shut down. That applies equally when the
  caller itself is cancelled (worker shutdown mid-upload). The change is
  additive and backward-compatible: `cancelled`
  defaults to `None`, which reproduces the old behaviour exactly, and the
  positional signature is unchanged. Consumers need not migrate anything —
  `upload_outputs` only sends `cancelled=` to an `upload_file` that declares
  it (or accepts `**kwargs`), so a client, test double, or
  `FakeBackendClient` subclass still written against
  `upload_file(task_id, filename, src)` keeps working instead of raising
  `TypeError` the moment a cancel guard is active (which is always — the
  worker keeps one running through output publishing, and `Worker(client=...)`
  accepts any duck-typed client). Such a client keeps the between-files-only
  cancellation it always had, and the SDK logs one WARNING per process per
  transfer direction naming the override. `FakeBackendClient.upload_file`
  mirrors the new keyword and raises `TaskCancelled` on a set event so the
  test double stays a faithful drop-in.
- `prepare_inputs` and `upload_outputs` now reject any `input_files` /
  `output_files` name that is not a plain basename. Both joined the
  caller-supplied name straight into a per-task sandbox directory
  (`work_dir/in/`, `work_dir/out/`, `shared_volume_path/temp/<task_id>/`), and
  a `Path` join has no notion of a sandbox: an `input_files` value of
  `../../x` from backend task params wrote outside the workdir onto the worker
  host, an escaped local output name staged onto the shared volume beside
  other cases' patient data, and an absolute `output_files` name discarded the
  output dir entirely — so remote mode read an arbitrary container file
  (`/etc/passwd`, an env file with credentials) and published it to the
  backend as a task output. Names containing path separators, URL
  delimiters/escapes, NUL or other platform-reserved characters; the bare `.`
  and `..` segments; drive/UNC prefixes; Windows device names; trailing dots
  or spaces; case-insensitive aliases; and empty or non-string values now
  raise `ProtocolError` naming the offending manifest key. Existing output
  sources must also be regular files, never symlinks. The whole manifest is
  validated **up front** — before the first download in `prepare_inputs`, and
  before any upload or copy in `upload_outputs` — so a bad entry can't publish
  the good entries listed ahead of it and then fail, leaving artifacts behind
  in the staging dir or on the backend.
  This intentionally tightens the file-manifest contract: values must be plain
  filenames, not relative descendants. Producers that previously returned a
  path must move the artifact into the task output directory and return its
  basename. Ordinary filenames (including dotted and dot-prefixed ones) pass
  through unchanged; the wire shape and public function signatures do not
  change.
- `Worker._run_one` now removes a leftover `task_<id>` directory at the start
  of every attempt, so a retry never inherits a dead attempt's workdir. The
  end-of-run cleanup only happens in `_run_one`'s `finally`; a mid-task kill
  (OOM killer, SIGKILL, host reboot) skips it while the container filesystem
  survives the restart, so the re-queued task's next attempt computed the same
  `task_<id>` path and found the dead attempt's tree still on disk. Inputs
  were then staged next to stale `in/` files, and — worse — `upload_outputs`
  publishes `out/<filename>` by name without checking who wrote it, so any
  output the retry's handler didn't (re)write was published as the retry's
  fresh result from the dead attempt's bytes. The removal **fails closed**:
  unlike the best-effort cleanup in `finally`, a leftover that can't be
  removed — permission error, filesystem fault, or a partial deletion that
  raises nothing but leaves siblings behind — aborts the attempt with a
  failure naming the workdir, instead of letting it run on top of another
  attempt's files and publish them. It runs off the event loop and after the
  heartbeat starts, so deleting a GB-scale leftover doesn't stall into the
  stale-task sweeper's window. No behaviour change for a first attempt: the
  directory doesn't exist and the call is a no-op. Nothing in the public API
  changes.
- `BackendClient.download_file` now accepts an optional keyword-only
  `cancelled` event and checks it before issuing the request and before
  writing each chunk, raising `TaskCancelled` at the next chunk boundary.
  `prepare_inputs` passes the `CancelGuard`'s event through, so a cancel is
  now visible *during* a remote input download instead of only between batch
  files. Previously the between-files check was the only cancel point in the
  remote path: a single-file input set — a lone colmap-splat PLY, a
  Neural-Canvas splat — has no such boundary, so a user cancel that arrived
  mid-download streamed the whole multi-GB file to completion before the
  worker noticed, burning minutes of bandwidth on a task nobody wanted.
  `TaskCancelled` is not a transient error, so it leaves the retry loop
  immediately without consuming retry budget (a retried cancel would
  re-stream the same file), and the partial file at `dest` is removed by the
  existing cleanup path. The change is additive and backward-compatible:
  `cancelled` defaults to `None`, which reproduces the old behaviour exactly,
  and the positional signature is unchanged. Consumers need not migrate
  anything — `prepare_inputs` only sends `cancelled=` to a `download_file`
  that declares it (or accepts `**kwargs`), so a client, test double, or
  `FakeBackendClient` subclass still written against
  `download_file(task_id, filename, dest)` keeps working instead of raising
  `TypeError` the moment a cancel guard is active (which is always — the
  worker starts one before staging inputs, and `Worker(client=...)` accepts
  any duck-typed client). Such a client keeps the between-files-only
  cancellation it always had, and the SDK logs one WARNING per process
  naming the override. `FakeBackendClient.download_file` mirrors the new
  keyword and raises `TaskCancelled` on a set event so the test double stays
  a faithful drop-in.
- `Worker`'s poll loop now doubles the idle wait after consecutive failed
  claim cycles, capped by the new `claim_backoff_max_s` keyword argument
  (default 60s). Any successful round-trip, including an empty queue, resets
  the wait to `poll_interval_s`; healthy polling is unchanged. When
  `retry_jitter` is enabled, escalated waits are sampled directly from the
  legal ±25% band so workers do not synchronize at the cap. Shutdown still
  interrupts the wait immediately. `poll_interval_s` and
  `claim_backoff_max_s` must now be finite positive values and invalid values
  fail at construction. The new argument is optional, but the validation
  tightens the existing constructor contract. Env-var and wire formats are
  unchanged. Before upgrading, verify any configured poll interval is finite
  and greater than zero.
- `BackendClient._retry` now honours the HTTP `Retry-After` header on a
  retryable status response (429/502/503/504, plus 500 on terminal
  reports). Both RFC 9110 forms are accepted — delta-seconds and
  HTTP-date — and the parsed value replaces the computed backoff. The
  server delay is independent of `retry_backoff_max_s`: shortening
  `Retry-After: 3600` to the default 60-second backoff cap would spend every
  attempt inside the closed window and strand a completed task
  `in_progress`. Remote input is instead capped by a dedicated six-hour
  project safety ceiling. `Retry-After: 0` and past
  HTTP-dates retry immediately; absent, malformed, and negative
  values fall back to the existing capped-jittered exponential schedule.
  With jitter enabled, server-directed retries receive positive-only jitter
  so fleet workers spread out without retrying before the named instant. The
  attempt budget is unchanged. No API, env-var, or wire-format changes.
- `BackendClient` now exposes a `poll_cancel_status` method: a one-shot
  GET `/tasks/{id}/cancel-status` with no retries and the dedicated
  `cancel_timeout_s` deadline. The `CancelGuard` (which polls this
  endpoint on its own `cancel_poll_interval_s` schedule, default 2s)
  now uses `poll_cancel_status` instead of `get_cancel_status`.
  Previously the guard's poll went through `get_cancel_status` →
  `_request` → `_retry`, retrying up to `max_retries` (4) times with
  exponential backoff. A degraded backend could blind the guard for
  up to `max_retries` × `cancel_timeout_s` plus backoff sleeps (~50s
  with default 4 attempts, 5s timeout, 2s base backoff) — during which
  the worker kept computing and uploading outputs for a task the user
  had already cancelled. The one-shot call fails fast (within
  `cancel_timeout_s`, default 5s): transport errors and transient HTTP
  status codes surface immediately, the guard catches them at DEBUG,
  and the next poll fires on schedule. Cancel detection drops from
  ~50s worst-case to `cancel_timeout_s` + `poll_interval_s` (default
  5s + 2s = ~7s). The change is additive and backward-compatible:
  `get_cancel_status` is unchanged (still retried via `_retry`), so
  any external callers are unaffected; `poll_cancel_status` is a new
  method. `FakeBackendClient` mirrors the new method so the test
  double stays a complete drop-in.
- `Worker._run_one` now links the `CancelGuard`'s `cancelled` event into
  the `ProgressReporter` via the new `ProgressReporter.link_cancelled()`
  method, so `ctx.progress.is_cancelled` and
  `ctx.progress.raise_if_cancelled()` reflect cancel detection at the
  guard's poll latency (`cancel_poll_interval_s`, default 2s) instead of
  the heartbeat's (`heartbeat_interval_s`, default 10s). Previously the
  `CancelGuard` polled `/tasks/{id}/cancel-status` on its own fast
  schedule but its `cancelled` event was separate from
  `ProgressReporter._state.cancelled` — the event that backs
  `ctx.progress.is_cancelled`. Handlers that poll `is_cancelled` between
  blocking ops (Neural-Canvas's segmentation pipeline, colmap-splat's
  `gs_build` subprocess watcher) only learned of a cancel on the next
  heartbeat tick, up to 10s after the guard already knew. Now the guard's
  detection propagates immediately: the linked event is checked alongside
  the heartbeat's own event in `is_cancelled`, so cooperative handlers
  bail out at guard latency. The change is additive and
  backward-compatible: `link_cancelled` defaults to `None` (no external
  event), so existing `ProgressReporter` callers outside `Worker` see no
  behaviour change; the heartbeat's own cancel-detection path is
  unchanged and still works as a fallback.
- `Worker.__init__` now exposes the retry-tuning parameters
  (`max_retries`, `retry_backoff_s`, `retry_backoff_max_s`, `retry_jitter`)
  and threads them through to the `BackendClient` it constructs internally.
  Previously these were only configurable via the undocumented `client=`
  escape hatch — most production deployments use `Worker(...)` directly and
  were locked into the default 4-attempt / 2s-base / 60s-cap / jitter-on
  policy with no way to adjust. Now a worker that talks to a backend that
  restarts frequently can raise `max_retries`, or a latency-sensitive worker
  with a low retry budget can lower it, all from the simple constructor. The
  defaults are unchanged (matching `BackendClient`'s defaults exactly), so
  existing consumers see no behaviour change. Consumers that supply their
  own `client=` are unaffected — the SDK still never reaches into an
  externally-supplied client. `BackendClient.retry_backoff_max_s`'s type
  annotation is widened from `float` to `Optional[float]` to reflect that
  `None` (disable the cap) was always a supported value; no runtime change.

**Fixes:**
- `BackendClient` now treats HTTP 429 (Too Many Requests) as a transient
  status code alongside 502/503/504, retrying it with exponential backoff.
  The shared backend serves 3+ workers (Neural-Canvas, Blender-CLI,
  colmap-splat); under burst load it can rate-limit a lifecycle call
  (complete/fail/progress). Previously a 429 on a terminal complete/fail
  surfaced immediately, dropping the status update and leaving the task
  stuck in_progress until the sweeper reclaimed it. Now the worker
  self-heals through a rate-limit blip the same way it rides through a
  gateway restart. The change is additive and backward-compatible: 429 was
  not previously retried, so no consumer relied on it surfacing
  immediately, and the exhaustion contract (re-raise the last
  `HTTPStatusError`) is unchanged.
- `BackendClient` now applies a dedicated `lifecycle_timeout_s` (default 15s)
  to `report_progress`, `complete`, and `fail`, instead of the 30s general
  request timeout. These are the worker's terminal-ish status calls; under
  backend load or during a deploy, a single stalled heartbeat or complete
  call could previously block the polling loop for up to 30s × `max_retries`
  (~120s with the default 4 attempts) — during which the worker couldn't
  claim new work, poll for cancel, or respond to shutdown. The shorter
  deadline fails fast: the retry loop still rides through transient blips
  (4 × 15s = 60s total instead of 4 × 30s = 120s), but the polling loop
  stays responsive under backend load. The 30s general timeout now governs
  only `claim_next`. This completes the timeout-separation pattern
  established by `cancel_timeout_s` (cancel-poll, 5s) and `file_timeout_s`
  (file transfers, 300s). `Worker` exposes the same `lifecycle_timeout_s`
  parameter (default `15.0`) and threads it through to the client. The
  change is additive and backward-compatible: existing consumers get the
  new 15s lifecycle deadline (a strict improvement in polling-loop
  responsiveness over the old 30s), and consumers that supply their own
  `client=` are unaffected. `lifecycle_timeout_s` also accepts `None` to
  fall back to the client's own timeout for consumers that don't want a
  separate lifecycle deadline.
- `BackendClient.get_cancel_status` now uses a dedicated short timeout
  (`cancel_timeout_s`, default 5s) instead of the 30s general request
  timeout. The `CancelGuard` polls `/tasks/{id}/cancel-status` every few
  seconds; under backend load or during a deploy, a single slow poll could
  previously block the guard for 30s (plus retry backoff) — during which the
  worker kept computing on a task the user had already cancelled. The short
  deadline fails fast: `CancelGuard` catches the `TimeoutException`, the next
  poll fires on schedule, and cancel detection drops from ~30s+ to ~5s. The
  30s general timeout still governs claim, heartbeat, complete, and fail
  unchanged. `Worker` exposes the same `cancel_timeout_s` parameter (default
  `5.0`) and threads it through to the client. The change is additive and
  backward-compatible: existing consumers get the new 5s cancel deadline (a
  strict improvement in cancel responsiveness over the old 30s), and
  consumers that supply their own `client=` are unaffected. `cancel_timeout_s`
  also accepts `None` to fall back to the client's own timeout for consumers
  that don't want a separate cancel deadline.
- `Worker._run_one` now keeps the `CancelGuard` active *during*
  `upload_outputs` and threads its `cancelled` event into the output-upload
  loop. Remote-mode `upload_outputs` (uploading `result["output_files"]`
  via `BackendClient.upload_file`) can spend minutes streaming GB-scale
  outputs — colmap-splat PLY files, Neural-Canvas splats. Until now the
  cancel guard was torn down as soon as the handler returned, so a user
  cancel during the upload window was completely invisible: the worker
  burned bandwidth and runtime uploading every remaining file to a task
  the user had already cancelled, then reported it complete. Now
  `upload_outputs` accepts an optional `cancelled: asyncio.Event` (matching
  the `prepare_inputs` contract) and checks it between batch uploads,
  raising `TaskCancelled` immediately when the guard detects a cancel. The
  `cancelled` parameter is keyword-only and defaults to `None`, so existing
  callers of `upload_outputs` are unaffected — the cancel-check is simply
  skipped.

- `Worker._run_one` now starts the `CancelGuard` *before* `prepare_inputs`
  and threads its `cancelled` event into the input-download loop. Remote-mode
  `prepare_inputs` (downloading `task.params.input_files` via
  `BackendClient.download_file`) can spend minutes streaming GB-scale inputs
  — colmap-splat PLY files, Neural-Canvas splats. Until now the cancel guard
  only started after staging finished, so a user cancel during that download
  window was completely invisible: the worker burned bandwidth and runtime
  downloading every remaining file, then ran the handler, and only discovered
  the cancel afterwards. Now `prepare_inputs` accepts an optional
  `cancelled: asyncio.Event` and checks it between batch downloads, raising
  `TaskCancelled` immediately when the guard detects a cancel. The same guard
  then stays active through the handler and upload phases as before. The
  `cancelled` parameter is keyword-only and defaults to `None`, so existing
  callers of `prepare_inputs` (including consumer repos that call it
  directly) are unaffected — the cancel-check is simply skipped.

- `Worker._run_one` now starts the progress heartbeat *before*
  `prepare_inputs` instead of after it. Remote-mode input staging
  (`prepare_inputs` downloading `task.params.input_files` via
  `BackendClient.download_file`) can take minutes for GB-scale inputs —
  colmap-splat PLY files, Neural-Canvas splats. Until now the heartbeat
  only began once staging finished, so the task row's `updated_at` stayed
  frozen at claim time for the entire download. If the backend's
  stale-task sweeper ran with a threshold shorter than the download
  duration, it marked the task stale and reclaimed it while the worker
  was still fetching — the worker then processed and completed a task the
  backend no longer owned, causing duplicate processing or silent result
  loss. Starting the heartbeat first means the backend receives
  continuous progress/heartbeat ticks throughout input staging. The
  `finally` block already calls `progress.stop()`, so a failure inside
  `prepare_inputs` still tears the heartbeat down cleanly (and a leaked
  heartbeat task can't double-start the next task's reporter). No public
  API change; existing consumers see only the improved liveness signal.

- `BackendClient` now uses a separate, longer timeout for file transfer
  operations (`download_file` / `upload_file`) instead of the single 30s
  general request timeout that previously governed every operation — claim,
  heartbeat, cancel-poll, and file transfers alike. Workers transferring
  GB-scale outputs (colmap-splat PLY files, Neural-Canvas splats) hit
  `WriteTimeout`/`ReadTimeout` on big files, exhausted retries inside the same
  30s window, and failed tasks that would succeed with a file-appropriate
  timeout. A new `file_timeout_s` parameter (default `300.0`) on
  `BackendClient` creates a dedicated `httpx.Timeout` applied per-request to
  the streaming download and multipart upload calls, leaving lifecycle latency
  (claim, heartbeat, cancel-poll) on the original 30s budget. `Worker` exposes
  the same `file_timeout_s` parameter (default `300.0`) and threads it through
  to the client. The change is additive and backward-compatible: existing
  consumers that don't override it get the new 300s file deadline (a strict
  improvement over the old 30s), and consumers that supply their own `client=`
  are unaffected (the SDK doesn't reach into an externally-supplied client).
  No env-var or wire-protocol change.
- `upload_outputs` now cleans up partial output artifacts when publishing
  fails partway through. Previously, if the Nth file copy/upload raised
  after files 1..N-1 had already been published, the partial artifacts
  were left behind as orphans: in local mode a half-populated staging dir
  (`shared_volume_path/temp/{task_id}/`) lingered indefinitely (the
  backend's sweeper only removes staging dirs for tasks it recorded as
  *complete*, and a failed task is retried from scratch); in remote mode
  the already-uploaded files sat on the backend with no trace. A failed
  task is retried, and without cleanup the retried run would re-publish
  over the partials — but if the retry also failed, or the task was
  cancelled, the orphans accumulated. The fix removes the whole staging
  dir on a local-mode copy failure (mirroring the
  `BackendClient.download_file` partial-file cleanup contract), and logs
  the orphaned remote-mode uploads at `WARNING` (there is no backend
  "delete output file" endpoint, so they are surfaced for operator
  reconciliation and overwritten on retry). The exception still
  propagates so the task is marked failed and retried cleanly. No public
  API change.
- `Worker._run_one` now logs at `ERROR` when the terminal `complete()` /
  `fail()` call fails after the `BackendClient`'s own retries are exhausted.
  Previously the `finally` block wrapped that call in a bare
  `except Exception: pass`, silently swallowing the failure: a task whose
  handler succeeded and whose outputs uploaded cleanly, but whose
  `complete()` call failed (backend down longer than the retry window, or a
  non-transient HTTP error), was left `in_progress` on the backend with zero
  operator visibility — the sweeper would eventually mark it stale, but no
  log line ever explained why. The fix surfaces the task id, the terminal
  method (`complete`/`fail`), the lost outcome, and the exception at `ERROR`
  level. The non-raising contract is preserved: a single failed terminal
  report must not kill the polling loop and strand every subsequent task.
  No public API change; existing consumers see new log lines only on the
  failure path that was previously silent.
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

- Dedicated unit tests for `ProgressReporter` (`tests/test_progress.py`),
  `CancelGuard` (`tests/test_cancel.py`), and the `watchdog` /proc-parsing
  + Phase-2-SIGKILL + sync_fail-exception paths (`tests/test_watchdog.py`).
  Previously these three modules had the lowest coverage in the package
  (`progress` 71%, `cancel` 74%, `watchdog` 61%) with no dedicated test
  files — they were exercised only indirectly through `Worker.run_one`
  integration tests, leaving the heartbeat loop, cancel-poll error
  tolerance, on_cancel hook exception handling, `/proc/<pid>/stat` parsing,
  descendant-tree walk, PID-reuse skip logic, and the SIGKILL escalation
  ladder untested. Overall package coverage rose from 88% to 95%;
  `progress` and `cancel` are now 100%, `watchdog` 97%.

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
- `ProgressReporter._heartbeat_loop` now escalates heartbeat-failure logging
  from DEBUG to WARNING after `heartbeat_warn_threshold` consecutive failures
  (default 3, configurable via `Worker(..., heartbeat_warn_threshold=N)` and
  `ProgressReporter(..., heartbeat_warn_threshold=N)`). A single failed
  heartbeat tick — the `BackendClient` has already exhausted its own retries
  by then — stays at DEBUG, since a transient blip during a long-running task
  is noise an operator doesn't need. But a *sustained* outage means the
  backend is unreachable and the task's `updated_at` is going stale, which
  the sweeper will soon read as abandonment and reclaim — wasting compute that
  is diagnosed minutes earlier once the failure is visible at the default
  (WARNING) log level instead of masked at DEBUG. The counter resets to 0 on
  the next successful tick, so a recovered backend doesn't keep warning. The
  change is additive and backward-compatible: the new parameter defaults to
  the prior-capable behaviour (DEBUG on failure) for the first (threshold−1)
  ticks and only adds WARNING escalation beyond that; consumers that never
  set `heartbeat_warn_threshold` see strictly more signal, never less.
- Local-mode file copies in `prepare_inputs` and `upload_outputs` no longer
  block the event loop. Both call sites used a single `shutil.copy2`, so a
  multi-GB copy (Blender-CLI `.blend` inputs, Neural-Canvas splats on a
  network-mounted shared volume) froze the loop for its entire duration: the
  heartbeat stopped ticking, so the backend's stale-task sweeper could reclaim
  a task the worker was actively copying in, and the `CancelGuard` poll froze
  with it, so a user cancel stayed invisible until the copy finished. Copies
  now run through a chunked helper that moves every filesystem operation off
  the event-loop thread and re-checks the `cancelled` event between chunks, so
  a slow read/write cannot freeze the heartbeat and a cancel aborts *mid-file*
  rather than only between files. Public API is unchanged (the helper is
  private): metadata semantics still match `copy2` via `copystat`, same-file
  copies raise `SameFileError`, a missing source still raises
  `FileNotFoundError` without deleting an existing destination, and any
  destination opened by a failed copy is removed before the exception
  propagates, mirroring the
  `BackendClient.download_file` partial-file cleanup contract. The cancel
  check runs after each read, so an already-complete copy is never discarded
  by a cancel detected during its final yield.
- Directory cleanups no longer block the event loop either. Two sibling
  `shutil.rmtree` calls were missed by the copy fix above: `Worker._run_one`
  removing the per-task workdir after every task, and `upload_outputs`
  removing the local-mode staging dir when a publish fails partway through.
  Both delete directories that hold GB-scale artifacts (colmap-splat PLYs,
  Neural-Canvas splats), and both ran synchronously on the loop — freezing the
  co-hosted FastAPI app in hybrid mode (`run_hybrid`), delaying the next claim,
  and stalling the heartbeat and `CancelGuard` poll of any work still in
  flight. Both now run via `asyncio.to_thread` with identical semantics:
  `ignore_errors=True` is preserved, so neither call raises, and the workdir /
  staging dir is removed exactly as before.

## v0.12.0 — 2026-07-17

**Features:**
- `CinematicBakingParams` now carries the optional Bioform material selection
  contract used by Blender-CLI and SynPusher: a stable lowercase
  `material_id`, plus a finite `pattern_scale` in `0.1..8.0` that is valid only
  when a material is selected. Existing producers remain compatible because
  both fields default to `None`; the generated TypeScript artifact exposes the
  same optional fields.

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
