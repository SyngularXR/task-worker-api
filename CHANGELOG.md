# Changelog

## Unreleased

**Features:**
- Add the dedicated `spatial_gs_build` task contract for independently training
  Gaussian Auras from completed spatial captures.
- Add the strict `spatial_recon` task params contract and packaged
  `coordinate_fixture_v1.json` for cross-repo anchor-space verification.

**Fixes:**
- `FakeBackendClient.complete` now runs the same encodability check as the
  real `BackendClient.complete` and re-raises the encoder's own exception when
  a result could not be sent. The real call raises while httpx *builds* the
  request — nothing is transmitted — which is why `Worker` pre-checks and
  converts it into a `fail()` report rather than orphaning the task
  in_progress until the sweeper recomputes hours of GPU work. The fake
  accepted any dict, so every consumer repo's handler unit tests passed on
  results production rejects: a stray numpy scalar, `Path` or `datetime` only
  surfaced on the box. The exception is passed through untouched — same class,
  message and traceback as production (`TypeError` for a `Path`, `ValueError`
  for a cycle, `UnicodeEncodeError` for an unpaired surrogate) — rather than
  rebuilt, which cannot drift from the real client and cannot fail on a value
  that is itself hostile to being formatted. Test suites
  that were passing unencodable results will now fail — that is the signal;
  fix the handler to return JSON types (`str(path)`, `dt.isoformat()`,
  `float(np_value)`). Confined to test code: no runtime or wire behaviour
  changes.

  The check asks the installed httpx instead of restating its rules, so the
  set it rejects tracks your pin, and the declared `httpx>=0.23` straddles a
  real boundary: 0.28 encodes with `allow_nan=False, ensure_ascii=False`, so a
  NaN metric and an unpaired surrogate raise there, while 0.23 — which
  SynPusher still pins — emits a bare `NaN` literal and a `\ud800` escape and
  sends both. The SDK's own tests for those two cases skip when the installed
  httpx encodes them, since matching the real client is the contract; pin
  httpx 0.28+ to have them caught.
- `BackendClient.__init__` now validates its four HTTP deadline knobs —
  `timeout_s`, `file_timeout_s`, `cancel_timeout_s`, `lifecycle_timeout_s` —
  raising `ValueError` naming the knob instead of handing the value straight
  to `httpx.Timeout`. Each degenerate value defeated the deadline it
  configured, silently at construction: `NaN` becomes an anyio deadline whose
  every comparison is False, so the request never times out — the same
  unbounded hang `_per_request_timeout` documents for a literal `None`,
  reached through a different door; `inf` disables the deadline outright; and
  a negative or zero value puts it in the past, so *every* request fails
  instantly — cancel polls always fail (`CancelGuard` blind for the whole
  task) and lifecycle writes always fail (terminal report lost, task orphaned
  `in_progress`). Consumers build these values from the environment, so a
  typo'd `.env` entry was a live path to a wedged or orphaned task. `None`
  remains legal for `file_timeout_s`/`cancel_timeout_s`/`lifecycle_timeout_s`
  where it is documented as "fall back to the client's own timeout";
  `timeout_s` is that client default and has nothing to fall back to. No
  default changed, and the checks run before the owned `httpx.AsyncClient` is
  built so a rejected config leaks no connection pool. The `Worker` forwards
  these knobs into both its home and foreign clients, so the client-side check
  covers both.
- `Worker.run_forever` now runs its fatal startup check inside the guarded
  region. The box-affinity verification ran after the periodic payload-log
  cleanup task was created but before the `try`, so a mismatch — the
  carefully-worded diagnostic for a typo'd `SYNPUSHER_URL` that would
  otherwise route trusted shared-volume paths at a foreign box — escaped with
  that task still pending, the home and every foreign client unclosed, and the
  payload logger never closed. The operator saw "Task was destroyed but it is
  pending" plus unclosed-socket warnings stacked on the one message that
  mattered. The existing `finally` now does the teardown it was written for.
- `Worker.__init__` now rejects duplicate foreign target URLs in
  `SYNPUSHER_TARGETS`, raising `ProtocolError` naming the repeated URL. A
  copy-pasted entry in a box's `.env.crossbox` built two `BackendClient`s
  against one backend, doubling that box's claim traffic and connection count
  every poll cycle and weighting it twice in the round-robin sweep. The home
  box was already rejected; a repeat is now equally visible — the container
  crash-loops instead of quietly double-polling. Both that check and the
  existing home-box check now compare URLs canonicalised by `httpx.URL`
  itself — the same resolution the client uses to dial, rather than a
  hand-written restatement of it — so lower-cased scheme and host, the
  scheme's default port dropped, `.`/`..` path segments resolved, the fragment
  ignored, and IDNA-encoded hosts folded together. `HTTP://FAR/api/v1`,
  `http://far:80/api/v1/`, `http://far/a/../api/v1`, `http://far/api/v1#frag`
  and `http://FÄR/api/v1` are now all recognised as the one backend they
  actually reach, instead of slipping past as distinct targets. URLs httpx
  cannot parse are still compared verbatim. No wire or `SYNPUSHER_TARGETS`
  format change.
- Foreign target polling in `Worker._claim` is now round-robin instead of
  fixed listed order. The sweep returns on the first successful claim, so with
  two or more boxes in `SYNPUSHER_TARGETS` a target with a standing backlog
  claimed every cycle and the targets after it were never polled at all — not
  merely deprioritised. The start index now advances one step per cycle, so
  each target gets first pick every `len(targets)` cycles, and home keeps its
  first refusal every cycle. Per-target backoff is now charged once per poll
  cycle rather than when the sweep reaches the target — since the sweep stops
  at the first claim, a target behind a backlogged one is never reached, and
  its counter would otherwise stall and stretch an N-cycle backoff to as much
  as `N * len(targets)` cycles. The backoff exponent is also bounded:
  `failures` increments forever against a permanently dead box, so
  `2 ** failures` built an ever-wider throwaway integer every cycle even
  though the result was always clamped to the 32-cycle ceiling. Worker-local
  and additive — no wire or
  `SYNPUSHER_TARGETS` format change.
- `Worker` now reclaims orphaned `task_<id>` scratch directories at startup
  and on the existing periodic cleanup timer. A process killed mid-task cannot
  run its normal `finally` cleanup, so GB-scale staging trees previously
  accumulated until operators removed them. Cleanup is deliberately narrow:
  only directories directly under the configured work root whose entire tree
  is older than 24 hours are removed; symlinks, active paths, newer trees, and
  other names are left alone. The age floor is configurable with
  `WORKER_WORKDIR_CLEANUP_MIN_AGE_S`. Active-path tracking is process-local, so
  scaled replicas that share a work root must keep the floor at least as long
  as their longest task duration.
- Non-finite task timeouts are now rejected instead of silently disabling the
  per-task watchdog. `WORKER_TASK_TIMEOUTS='gs_build=nan'` parsed cleanly
  (`float('nan')` succeeds), `resolve_task_timeout` returned `NaN`, and
  `_run_one`'s `timeout_s > 0` gate is `False` for `NaN` — so the watchdog was
  never started and a wedged handler ran unbounded with no log line saying
  why, on exactly the task types operators bother to configure timeouts for;
  `inf` reached `TaskWatchdog` with a deadline that never arrives, for the
  same effect. `parse_timeouts_env` now skips non-finite entries with the same
  WARNING it already emits for malformed ones (so the documented default
  applies instead), and the `Worker` constructor rejects non-finite
  `task_timeout_s` / `task_timeouts` values with a `ValueError`, like its
  pacing knobs. `<= 0` remains the documented "no timeout" escape hatch, and
  ints keep working — only a consumer already passing a broken value sees the
  new `ValueError`.
- `Worker` now validates its remaining pacing knobs — `heartbeat_interval_s`,
  `cancel_poll_interval_s` and `timeout_grace_s` — through the same
  finite-and-positive guard that already covered `poll_interval_s` and
  `claim_backoff_max_s`. Each was silently accepted at construction and paid
  for in production, against the one backend the whole fleet shares.
  `heartbeat_interval_s <= 0` turned the heartbeat loop's `asyncio.sleep` into
  a no-op, so the worker hammered `PUT /tasks/{id}/progress` as fast as the
  backend could answer; `cancel_poll_interval_s <= 0` did the same to the
  cancel guard's `GET /tasks/{id}/cancel-status`. `timeout_grace_s=NaN` made
  `TaskWatchdog._wait(nan)` return instantly at *both* grace phases (`end =
  now + nan`, so the loop body never runs), collapsing SIGTERM → grace →
  SIGKILL → grace → hard-exit into an immediate `os._exit(75)` container kill
  on the first deadline. `inf` is rejected for the same reason it is on the
  poll knobs: the wait never ends, so the worker never heartbeats, never
  notices a cancel, and never reaches the hard exit. Defaults (10s / 2s / 15s)
  and wire formats are unchanged, and ints keep working — only a consumer
  already passing a broken value sees the new `ValueError`.
- `BackendClient` now validates `retry_backoff_s`, the base of its
  exponential-backoff schedule (`retry_backoff_s * 2**n`) and the last retry
  knob with no guard on it — `max_retries`, `retry_backoff_max_s` and
  `retry_sleep_budget_s` each already fail fast at construction. Every
  degenerate value disabled or broke the backoff it configures, silently at
  construction and visibly only once a transient failure landed in production.
  A **negative** base made every delay negative: `retry_backoff_max_s` only
  clamps from above and jitter is skipped on a non-positive delay, so
  `asyncio.sleep` returned immediately on every attempt and the full attempt
  budget fired back-to-back at an already-failing backend — un-spaced, and
  across the fleet un-decorrelated, which is the retry storm the schedule
  exists to prevent. **NaN** failed every comparison, passed the cap untouched
  and reached `asyncio.sleep`, which rejects it with `ValueError: Invalid
  delay: NaN`; that is neither a retryable exception nor an `HTTPStatusError`,
  so it escaped `_retry` mid-loop and replaced the transient error the caller
  was meant to handle. **inf** clamps to the cap when there is one, but
  `retry_backoff_max_s=None` is supported, and uncapped the jitter band is
  `uniform(inf - inf, inf)` — NaN again. A finite base `>= 0` is now required;
  `0` remains legal (retries with no inter-attempt sleep) and the SDK default
  of `2.0` is unaffected, so only a consumer already passing a broken value
  sees the new `ValueError`.
- `BackendClient`'s per-call deadlines no longer disable timeouts entirely when
  a consumer opts out of one. `file_timeout_s`, `cancel_timeout_s` and
  `lifecycle_timeout_s` each document `None` as "fall back to the client's own
  timeout", and each passed that `None` straight to httpx as the request's
  `timeout=`. httpx resolves a per-request timeout in `build_request`, where an
  explicit `None` becomes `Timeout(None)` — connect, read, write and pool all
  disabled, i.e. **no timeout at all** — so every documented opt-out was in fact
  an unbounded request against an unresponsive backend, and one below the level
  `_retry` can see (the hang is inside a single attempt, so there is never a
  failure to retry). A cancel poll that never returns leaves the `CancelGuard`
  blind for the rest of the task, so a user cancel is never acted on; a
  heartbeat or terminal `complete`/`fail` that never returns wedges the worker's
  single-task polling loop — no claims, no cancel polls, no shutdown response —
  while the sweeper reclaims the task as abandoned; a `download_file` that never
  returns strands its partial file at `dest`. The fallback is now httpx's
  `USE_CLIENT_DEFAULT` sentinel, which is the only spelling that actually
  inherits the client's timeout. SDK defaults were never affected (`Worker`
  passes 300s/5s/15s), so this changes behaviour only for consumers that
  explicitly opted a deadline out — and only by bounding a call that previously
  could not be bounded.
- `BackendClient.download_file` now removes its partial file when the *caller*
  is cancelled, not just when the download itself fails. The cleanup caught
  `Exception`, and `asyncio.CancelledError` is not one, so a worker shutting
  down (or a task watchdog unwinding a run) mid-transfer left a truncated file
  at `dest`. `prepare_inputs` stages into a stable per-task input dir, so that
  truncated file is one a retried task can pick up as a complete input. The
  cleanup now catches `BaseException` — the same contract `files._copyfile_async`
  already applied to the local-mode copy, and which its docstring already
  described as mirroring `download_file`.
- `BackendClient` now treats **408 Request Timeout** as a transient status and
  retries it with the same exponential backoff as 429/502/503/504. A 408 is
  not a client error in the sense the rest of the 4xx range is: it is the
  gateway reporting that a request's headers or body did not arrive inside
  `client_header_timeout`/`client_body_timeout` — the same slow-link blip
  already retried when it surfaces as an `httpx.TimeoutException` instead of
  a status, and RFC 9110 §15.5.9 says the client "MAY repeat the request
  without modifications at any later time". The case that hits it is
  `upload_file` streaming a GB-scale output (a colmap-splat PLY, a
  Neural-Canvas splat) over a congested link: the upload failed outright on
  the timeout, failing a task whose GPU work was already finished. Every
  route this client calls is an idempotent guarded transition, and
  `upload_file` re-opens `src` per attempt, so the retry re-sends the whole
  file rather than an already-consumed handle. The other 4xx codes are
  unchanged — still non-transient, still surfaced immediately.
- Jitter no longer pushes a retry delay past `retry_backoff_max_s`. The ±25%
  spread was applied to the capped delay and handed straight back, so any
  delay from `0.8 * max_s` upward — every delay once the exponential has
  climbed to the ceiling — could return as much as `max_s * 1.25`: a worker
  that set a 60s cap to bound how long one call may block its single-task
  polling loop got up to 75s of blocking from it. The jitter *band* is now
  clipped to `max_s`, so the returned delay never exceeds the cap while
  staying a continuous spread (`[0.75 * max_s, max_s]` at the ceiling).
  Clamping the drawn value instead would have honoured the cap but piled
  every over-cap draw onto `max_s` exactly, re-synchronising the workers that
  jittered upward — the thundering herd jitter is there to break up. This is
  the same band-clipping `Worker._claim_wait_s` already does for the claim
  backoff. Delays whose whole band fits under the cap are unchanged, and
  `retry_jitter=False` is unaffected.
- A task whose handler produced a result the wire can't encode no longer
  orphans in `in_progress`. The classic trigger is a stray numpy scalar,
  `Path` or `datetime` left in a handler's dict: `complete()` raised while
  *building* the request — nothing was ever sent, and no amount of retrying
  would have helped — `Worker._run_one` logged the failure at ERROR and moved
  on, so the backend went on believing the task was still running until its
  stale-task sweeper reclaimed and **recomputed** it (hours of GPU work thrown
  away, and until then a task stuck "running" in the UI). The worker now
  checks the result against httpx's own encoder before the call and, when it
  can't be encoded, reports `fail()` with the encode error instead — the task
  lands terminal and the operator sees the real cause.
  Checking *before* the request is what makes this safe, and is why there is
  deliberately no fallback after a failed `complete()`: once the request is on
  the wire its failure is ambiguous (the write may have committed with only
  the response lost), and reading the task back doesn't close that window —
  the write can still commit, or a cancel/requeue land, between the read and
  the `fail()`, stamping `failed` over a real outcome. Ruling that out needs
  an atomic conditional transition or a backend idempotency contract that this
  independently-shipped SDK can't verify at runtime, so a genuinely ambiguous
  terminal-report failure keeps its existing behaviour: one ERROR log, no
  second write. Still non-raising — a failed report must not kill the polling
  loop. Purely worker-side: no API, wire-format, or consumer-visible contract
  change.
- `BackendClient.download_file` no longer blocks the event loop while writing
  to disk. It streamed straight from `aiter_bytes()` — whatever the transport
  handed over, typically ~64 KB — and wrote each chunk inline, along with
  opening and closing `dest`. On slow or network-mounted storage a multi-GB
  input (a colmap-splat PLY, a Neural-Canvas splat) therefore froze the loop
  for the whole transfer: the heartbeat stopped ticking, so the backend's
  stale-task sweeper read the frozen `updated_at` as abandonment and reclaimed
  a task the worker was actively downloading for; the `CancelGuard` poll froze
  with it, so a user cancel stayed invisible; and in hybrid mode the worker's
  FastAPI app stopped serving requests. Every filesystem call — open, each
  write, close — now runs through `asyncio.to_thread`, mirroring
  `files._copyfile_async` (which fixed the same bug class for local-mode
  copies), and wire chunks are accumulated into a 1 MB buffer so the thread
  dispatches stay proportional to the file size instead of to the transport's
  chunking. The stream is still *iterated* at the transport's own granularity,
  so `cancelled` is checked on every chunk that arrives and the cancellation
  boundary is unchanged. No API change — same signature, same bytes, same
  retry/cleanup/cancel semantics.
- `CancelGuard` no longer silently loses cancel detection on a duck-typed
  client that predates `BackendClient.poll_cancel_status`. The guard's poll
  loop called `client.poll_cancel_status(task_id)` unconditionally; on a
  legacy client (a worker repo's own client, a test double, a
  `FakeBackendClient` subclass — `Worker(client=...)` accepts any of them)
  every tick raised `AttributeError`, which the loop swallows at DEBUG — so
  the `cancelled` event never set, the `on_cancel` hook never fired (a
  subprocess handler like Blender/colmap was never terminated), and the
  event threaded into `prepare_inputs`/`upload_outputs` never aborted a file
  transfer. The guard now falls back to the retried `get_cancel_status` for
  such clients — exactly the behaviour it had before the one-shot poll
  existed — and logs one WARNING per process naming the method to add,
  mirroring the `report_progress_once` fallback. **Additive and
  backward-compatible**: clients with `poll_cancel_status` (including the
  SDK's own `BackendClient`) are unaffected and keep the one-shot poll.
- `Worker._run_one` now keeps the progress heartbeat running *through* the
  terminal `complete()`/`fail()` report, instead of stopping it just before.
  Those calls retry inside `BackendClient`, so against a degraded backend one
  report can span minutes of attempts and backoff — and with the heartbeat
  already stopped, the task's `updated_at` stayed frozen for that entire
  window. That is exactly what the backend's stale-task sweeper reads as
  abandonment: it reclaimed and re-queued a task this worker was still
  reporting on, a second worker claimed it, and both computed and published
  the same outcome in parallel. Heartbeat ticks during the report are what
  tell the sweeper the worker is alive and finishing. The report block is
  wrapped in `try`/`finally` so `progress.stop()` still runs if the report
  raises something its own `except Exception` doesn't catch, or if the worker
  task is cancelled mid-report — a leaked heartbeat would outlive the task and
  make the next task's `start_heartbeat()` reject a double start. No API
  change; consumers get the new ordering by upgrading.
- `ProgressReporter.update()` no longer stalls a handler on a degraded
  backend. The immediate progress report it emits on every stage transition
  runs on the handler's critical path, but went through the retried
  `BackendClient.report_progress` — so one update could block the handler for
  `max_retries` × `lifecycle_timeout_s` plus backoff sleeps (~75s with the
  SDK defaults: 4 attempts, 15s lifecycle timeout, 2s base backoff) while the
  work it was describing sat idle. `update()` now calls a new
  `BackendClient.report_progress_once` — a one-shot PUT to the same
  `/tasks/{id}/progress` endpoint, same body, same `lifecycle_timeout_s`
  deadline, no retry loop — so a stalled report costs at most one 15s
  timeout. Dropping a single immediate report only costs stage-transition
  latency: the state is already in the reporter, and the **background
  heartbeat still uses the retried `report_progress`**, which is what keeps
  `updated_at` fresh through a backend blip so the sweeper doesn't read the
  task as abandoned. Errors surface to `update()`'s existing WARNING, exactly
  as an exhausted retry budget already did. This mirrors what
  `poll_cancel_status` did for the `CancelGuard`'s hot poll path.
  **Additive and backward-compatible**: a duck-typed client without
  `report_progress_once` (a worker repo's own client, a test double, a
  `FakeBackendClient` subclass — `Worker(client=...)` accepts any of them)
  keeps the retried call it always had, and logs one WARNING per process
  naming the method to add. `FakeBackendClient` gained the method, delegating
  to `report_progress` so subclasses that override it still see both paths.
- `BackendClient` (and `Worker`, which forwards it) takes a new optional
  `retry_sleep_budget_s` — a budget for the time one call may spend *sleeping*
  between retry attempts. **It defaults to `None`, which is
  the pre-existing unbounded behaviour: upgrading the SDK alone changes
  nothing.** 600 seconds is the recommended value, and must be enabled
  deliberately, per consumer — see
  [`docs/fleet/runbooks/sdk-upgrade.md`](docs/fleet/runbooks/sdk-upgrade.md)
  for the staged rollout. The existing caps bound each delay in isolation, but
  they multiply against the attempt budget, and a `Retry-After` delay is
  honoured in full by design (bounded only by its own six-hour remote-input
  ceiling, never shortened by `retry_backoff_max_s`). So a persistently
  rate-limited backend can hold a single terminal `complete`/`fail` report —
  which floors its attempt budget at 6 attempts, and so at five inter-attempt
  sleeps — for up to 5 × 6h = 30h. A worker runs one task at a time, so that
  one call blocks the entire polling loop for the duration: no new claims, no
  cancel polls, no response to shutdown, and nothing in the logs to explain
  it. With a budget set, `_retry` accumulates the time it has slept across
  attempts — measured on the monotonic clock around each sleep, so a sleep
  that overruns its requested delay (a task handler blocking this process's
  event loop) is charged what it actually cost rather than what it asked for —
  and, when the next required delay would not fit in what remains of the
  budget, stops and re-raises the last error rather than sleeping on and then
  firing a near-certainly-futile request inside the rate-limit window. The
  outcome for the caller is identical to exhausting the attempt budget — for a
  terminal report the task is re-queued by the backend's sweeper exactly as
  any retries-exhausted report is today — and the worker gets back to polling.
  Early exhaustion logs one WARNING naming the elapsed backoff, the delay that
  didn't fit, and the budget, so an operator can tell this apart from a
  backend that is actually down. A delay landing exactly on the budget still
  fits. **It bounds admission, not wall clock**: the loop refuses to *start* a
  sleep that would not fit, but never interrupts one already in flight, so a
  starved event loop overruns the budget by however long it was blocked — a
  600s budget whose sleeps each take twice their delay stops after ~800s.
  Measuring on the monotonic clock makes every later admission see the real
  spend, but nothing running inside a blocked loop can preempt the sleep
  itself (an `asyncio.wait_for` timer is starved by the same block), so the
  name says budget rather than max: leave headroom for a handler that blocks.
  The knob is validated at construction: a finite number `> 0`, or
  `None` to opt out. `nan` is rejected specifically because it would pass a
  bare `> 0` check and then silently disable the very budget it was asked to
  impose (every comparison against `nan` is False), and `inf` is an unbounded
  budget spelled as a number. `retry_backoff_max_s` keeps its existing
  validation unchanged.
- `BackendClient.upload_file` now accepts an optional keyword-only
  `cancelled` event: it is checked before the PUT goes out, and the in-flight
  request is raced against it so a cancel arriving mid-upload aborts the
  request and raises `TaskCancelled` instead of streaming the rest of the
  body. The cancel race covers the complete transfer retry loop, so it also
  interrupts retry backoff instead of waiting for the next attempt.
  Cancellation stops the client transport; it cannot retract a file the
  backend finished committing before the connection was severed.
  `upload_outputs` passes the `CancelGuard`'s event through, so a
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
- `upload_outputs`' partial-publish cleanup now survives caller cancellation.
  Both handlers caught `Exception`, and `asyncio.CancelledError` is not one, so
  a worker shutdown (`run_hybrid` cancelling the worker task) or a task
  watchdog unwinding a run *during output publishing* skipped the cleanup
  entirely: the local-mode staging dir `<shared volume>/temp/<task_id>` was
  left holding the GB-scale artifacts already copied. The backend only sweeps
  staging dirs for tasks it recorded as complete, so that orphan is permanent
  and accumulates on the shared volume. Both handlers now catch
  `BaseException` and re-raise unchanged — the same contract, and the same
  reasoning, as `files._copyfile_async` and `BackendClient.download_file`.
  Which exceptions propagate is unchanged; remote mode still only logs its
  partial-upload WARNING (there is no backend delete-output route).

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
