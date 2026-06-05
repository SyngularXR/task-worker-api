# Per-task execution timeout — design

**Status:** approved (2026-06-05)
**Scope:** `task_worker_api` SDK (`worker.py`). One change; every consuming worker
(Blender-CLI, colmap-splat, Neural-Canvas, assetbundle-builder) inherits it via the
normal SDK-pin bump.

## Context / problem

The `Worker` runs tasks sequentially: `run_forever()` → `_claim()` → `_run_one()`,
where `_run_one` does `result = await handler(ctx, params)` under a `CancelGuard`.
There is **no upper bound on how long a single task may run.**

Observed in production (bm04, 2026-06-05): a `blender-worker` task
(`mesh-preprocessing` / convex-hull of a pathological vessels mesh) ran away —
the `blender-pipe` subprocess burned 3.5h of CPU and never returned. Because that
handler **blocked the asyncio event loop**, the worker's heartbeat stopped and
nexus-core marked the worker `offline` for over an hour, while the container still
showed `up` (no healthcheck catches a wedged worker loop). All other blender work
queued behind it.

Cancellation today is cooperative only (`CancelGuard` raises `TaskCancelled` at the
next `await`) and runs **on the event loop** — so it cannot interrupt a handler that
blocks the loop. The same is true of any `asyncio.wait_for`-based timeout.

## Goals

- A task that exceeds a deadline is terminated automatically by the worker.
- Robust to handlers that block the event loop (the failure we actually hit).
- The worker self-heals (resumes polling) without operator intervention.
- Default 30 min, tunable per task type without a code change.

## Non-goals

- Retrying timed-out tasks (explicitly out — see Decisions).
- Backend-side enforcement (nexus-core) — it can't kill the local runaway process.
- Fixing individual handlers to be non-blocking (separate, larger work; the timeout
  must work regardless).

## Decisions (locked)

1. **Default 30 min + per-type overrides**, config/env-driven. A flat cap is unsafe
   because `gs_build` (COLMAP + gaussian-splat reconstruction) and `render` can
   legitimately exceed 30 min.
2. **Timed-out task → terminate + `client.fail(id, "timeout: exceeded Ns")`,
   terminal, no worker-side retry.** Avoids re-stick loops on pathological inputs
   (the vessels mesh would just time out forever on retry). The failure is visible
   in the admin UI for a human; the backend may apply its own policy.
3. **Enforcement = OS watchdog thread** (not asyncio), so the deadline fires even
   when a handler blocks the loop.
4. **Kill scope = task-spawned children only** (eng review). Snapshot the worker's
   child PIDs at task start; at timeout target only PIDs that appeared since.
   Minimizes blast radius vs killing all descendants.
5. **In-process hangs get a hard-exit last resort** (eng review). With no subprocess
   to kill (e.g. a wedged in-process GPU call in a `run_hybrid` worker), the watchdog
   makes a best-effort synchronous `fail(timeout)` then calls an injectable hard-exit
   (`on_hard_exit`, default `os._exit`); `restart: unless-stopped` (confirmed on all
   four workers) brings it back. Accepts ~seconds of neural-canvas API downtime on
   that rare event.
6. **Deadline covers the whole task** (eng review) — the watchdog starts at claim, so
   input staging (`prepare_inputs`) and output upload are inside the window, not just
   the handler.

## Design

### Configuration

Resolved per task at claim time.

- Constructor: `task_timeout_s: float = 1800.0` (global default) and
  `task_timeouts: dict[TaskType, float] | None = None` (per-type overrides).
- Ops env (one var per worker service): `WORKER_TASK_TIMEOUTS`, a comma-separated
  list of `key=seconds` where `default` sets the global and task-type values
  override, e.g. `WORKER_TASK_TIMEOUTS="default=1800,gs_build=7200,render=5400"`.
- Resolution order for a task type: env per-type → constructor per-type →
  env `default` → constructor `task_timeout_s`.
- A resolved value `<= 0` **disables** the timeout for that type (escape hatch for
  a known-unbounded type). Disabled = today's behavior exactly.
- The effective value is logged at task start: `task 226: gs_build timeout=1800s`.

### Mechanism — watchdog thread + escalation ladder

`_run_one` resolves the timeout, then (if enabled) starts a per-task daemon
`TaskWatchdog` thread holding `deadline = time.monotonic() + timeout`. The thread
sleeps in short ticks and exits early when the task signals completion. At task start
the SDK snapshots the worker's current child PIDs; on timeout it targets only PIDs
that appeared since (**task-spawned children**) — handler-agnostic, no per-handler
wiring, minimal blast radius.

On expiry the watchdog escalates:

1. **Mark** a `timed_out` flag for the current task. `_run_one` consults this flag
   and reports `client.fail(id, "timeout: exceeded {N}s")` regardless of what the
   handler ultimately returns or raises.
2. **SIGTERM** the task-spawned child processes (the start-snapshot delta; enumerated
   via `psutil` `Process(os.getpid()).children(recursive=True)` or a `/proc` walk —
   workers are Linux containers). Wait `grace_s` (default 15s).
3. **SIGKILL** any survivors. Killing the child unblocks the handler's blocked wait
   (`subprocess.run` / `proc.communicate` / `proc.wait` returns) → control returns
   to `_run_one` → it reports the timeout failure → the loop resumes polling.
4. **Last resort** — in-process runaway with no task-spawned child to kill (e.g. a
   wedged CUDA call inside a Neural-Canvas threadpool, which a thread cannot be
   force-killed out of): after a final grace with the task still not done, the
   watchdog makes a **synchronous best-effort** `fail(timeout)` HTTP call (plain
   `urllib` — the async client is on the blocked loop) and then calls an **injectable
   hard-exit** (`on_hard_exit`, default `os._exit`). `restart: unless-stopped`
   (confirmed on all four workers) brings it back. For `run_hybrid` workers
   (neural-canvas) this also drops the in-process FastAPI app for the few seconds
   until restart — accepted, since the alternative is an unbounded wedge. The
   injection point keeps this path unit-testable (assert the callable is invoked
   rather than killing the test runner).

### Control flow

```
_run_one(task):                                          # runs on the event loop
    timeout = resolve_timeout(task.task_type)            # 0 → disabled (today's behavior)
    if timeout > 0:
        wd = TaskWatchdog(timeout, grace_s, on_hard_exit, loop, sync_fail)
        wd.start(children_at_start=snapshot_children())  # covers staging→handler→upload
    try:
        ... stage inputs → handler under CancelGuard → upload outputs ...
        report = ('complete', result)
    except TaskCancelled:   report = ('fail', 'cancelled by user')
    except Exception as e:  report = ('fail', format_error(e))
    finally:
        fired = wd.stop() if timeout > 0 else False      # non-blocking; True iff deadline hit
        if fired: report = ('fail', f'timeout: exceeded {timeout}s')
        await emit_terminal(report)                      # CAS-guarded async report (loop side)
```

### Thread ↔ loop boundary (correctness — codex review)

The watchdog runs in an OS thread and must never touch loop-bound objects:

- It **only kills processes** + sets a `timed_out` flag. It does **not** call the
  async `BackendClient` (`httpx.AsyncClient` is bound to the loop; a blocked loop means
  `run_coroutine_threadsafe` never runs).
- **Normal / subprocess case:** killing the task-spawned child unblocks the handler →
  control returns to `_run_one` → the `finally` sees `fired` and reports via the async
  client on the (now free) loop.
- **Hard-exit case only:** if the task still has not completed after a final grace
  (in-process wedge; loop never resumed), the watchdog does a **standalone synchronous**
  `fail(timeout)` (plain `urllib`, explicit ~3s timeout so it cannot itself wedge) then
  calls `on_hard_exit`. This path skips the normal `finally` cleanup (task dir,
  `progress.stop`); the container restart clears `/tmp`.
- `emit_terminal` (loop) and the watchdog's sync-fail share one lock-protected
  `terminal_reported` flag → **exactly-once, first-resolver-wins.** "Timeout wins" is
  *not* guaranteed and isn't needed: if the task genuinely finished a millisecond before
  the deadline, reporting success is correct.
- `_run_one` captures the running loop up front and hands it to the watchdog; the
  watchdog never calls `asyncio.get_event_loop()`.

Deadline is `monotonic()`-based, independent of heartbeats. `CancelGuard` and heartbeat
paths are unchanged; the watchdog is additive.

### Edge cases

- Task finishes before the deadline → `finally` stops/joins the watchdog; no kill.
- One task at a time → exactly one watchdog; no cross-task interference.
- Clean worker shutdown → watchdog is a daemon thread and is stopped/joined.
- `fail()` after a kill: when children are killed and the loop unblocks, the normal
  async `fail` path runs. Only the in-process-exit branch needs the sync `fail`.
- Disabled (`<= 0`) → no watchdog created.
- Double-report guard (Q1): one lock-protected `terminal_reported` CAS gates both the
  loop's report and the watchdog's sync-fail → exactly-once, first-resolver-wins.
- A hang in `prepare_inputs` (staging) past the deadline is terminated too, since the
  watchdog starts at claim, not at handler entry.
- PID reuse: task-spawned children are matched by (PID, process `create_time`), not PID
  alone, so a recycled PID can't be mis-targeted under churn.
- `os._exit` (hard-exit path) skips `finally` cleanup — task dir + `progress.stop` are
  left for the container restart to clear. Stated, not silent.

## Testing

- Fake long handler (sleeps past a tiny test timeout) → asserts terminate +
  `client.fail("timeout…")` within `timeout + grace`, via `FakeBackendClient`.
- Subprocess case: handler spawns `sleep 600` → asserts the child is killed and the
  task is failed.
- Blocked-loop case: handler does a synchronous `time.sleep` (blocks the loop) →
  asserts the watchdog thread still fires and acts (validates the core fix).
- In-process last resort: inject a fake `on_hard_exit`; a no-subprocess hang past the
  deadline → asserts best-effort `fail(timeout)` then `on_hard_exit` invoked (no real
  process exit in the test).
- Double-report race: completion and watchdog-fire interleaved → asserts exactly one
  terminal report, timeout wins.
- Staging coverage: a hang in `prepare_inputs` past the deadline is terminated.
- Config resolution: env per-type / constructor per-type / env default / constructor
  default precedence; `0` disables.
- Existing suite stays green.

## Rollout

1. Implement in `task_worker_api`; bump SDK `0.6.1 → 0.7.0` + CHANGELOG; merge →
   CI publishes the wheel (per repo CLAUDE.md release flow).
2. Bump the SDK pin in the four worker repos (`docs/fleet/runbooks/sdk-upgrade.md`).
3. In surgiclaw-deploy, set `WORKER_TASK_TIMEOUTS` per worker service in
   `bundle/compose/docker-compose.yml` (colmap-splat gets the `gs_build` override).
4. Publish a new bundle + deploy; the per-service timeout takes effect.

## Known limitations (codex review — accepted)

The in-process watchdog is a pragmatic, SDK-only fix. It fully covers the observed
failure (a subprocess handler that blocks the loop — the blender wedge) and the common
cases, but it does not pretend to be airtight:

- **GIL-holding busy loop:** a pure-Python `while True:` (or a C extension that never
  releases the GIL) starves the watchdog thread itself — it cannot even `os._exit`. Most
  real hangs release the GIL (subprocess waits, `time.sleep`, socket/file I/O, most CUDA
  ops), so this is narrow, but real.
- **Child attribution in hybrid workers:** the start-snapshot delta assumes children
  appearing during the task belong to the task. In a `run_hybrid` worker (neural-canvas)
  the co-resident FastAPI app could in principle spawn a child mid-task that the delta
  would also target. Low risk for the current fleet (neural-canvas does in-process GPU
  work, not subprocesses), but noted.
- **Double-fork / reparenting:** a grandchild that double-forks and reparents to PID 1
  escapes the descendant set. Before SIGKILL the watchdog re-enumerates to catch
  late-spawned descendants, but a deliberately-detached daemon can still escape.

The robust answer to all three is **process-per-task** (run each handler in its own
process group, parent kills the group) — see NOT in scope; filed as a hardening TODO.

## What already exists

- `CancelGuard` (cancel.py) — cooperative cancel poller with an `on_cancel` hook.
  Reused conceptually (a timeout is "cancel-like") but it is event-loop-bound, so it
  cannot stop a handler that blocks the loop — which is exactly why the watchdog is a
  separate OS thread. Not rebuilt.
- `ProgressReporter` heartbeat (progress.py) — unchanged; the timeout is wall-clock
  and independent of heartbeats.
- `BackendClient.fail()` / `.complete()` (client.py) — reused for terminal reporting;
  the last-resort path adds a small synchronous `fail` (plain `urllib`) for when the
  event loop is blocked.

## NOT in scope

- Retrying timed-out tasks — explicitly terminal-fail (Decision 2).
- Backend (nexus-core) timeout enforcement or a distinct `timed_out` task status —
  deferred; the worker reports a normal `fail` with a `timeout:` reason.
- Making individual handlers non-blocking / fully async — larger per-repo work; the
  watchdog must work regardless of handler behavior.
- **Process-per-task execution model** (codex's "best design": run each handler in its
  own child process group, parent owns timeout/kill/heartbeat/fail). Eliminates the
  thread↔loop, PID-attribution, and GIL-hang limitations above, but is a worker-contract
  rewrite touching all four workers + IPC for ctx/files/progress. Deferred as a hardening
  TODO if the documented limitations ever bite in practice.
- A worker-loop healthcheck that detects a wedged loop — orthogonal to auto-kill
  (would have surfaced the blender wedge faster). Candidate TODO.
- Force-killing a non-cooperative in-process thread without exiting the process — not
  possible in CPython; the hard-exit last resort is the accepted substitute.

## Open questions

None. Grace period default 15s, env-var format `default=…,type=…`, kill-scope
(task-spawned only), and the hard-exit last resort were all confirmed during design
and eng review.
