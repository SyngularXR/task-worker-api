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
sleeps in short ticks and exits early when the task signals completion. The worker
runs one task at a time, so at most one watchdog is live and **all descendant
processes of the worker belong to the current task** — no per-handler wiring needed.

On expiry the watchdog escalates:

1. **Mark** a `timed_out` flag for the current task. `_run_one` consults this flag
   and reports `client.fail(id, "timeout: exceeded {N}s")` regardless of what the
   handler ultimately returns or raises.
2. **SIGTERM** the worker's descendant processes (enumerated via `psutil`
   `Process(os.getpid()).children(recursive=True)`, or a `/proc` walk to avoid the
   dependency — workers are Linux containers). Wait `grace_s` (default 15s).
3. **SIGKILL** any survivors. Killing the child unblocks the handler's blocked wait
   (`subprocess.run` / `proc.communicate` / `proc.wait` returns) → control returns
   to `_run_one` → it reports the timeout failure → the loop resumes polling.
4. **Last resort** — in-process runaway with no child to kill (e.g. a wedged CUDA
   call inside a Neural-Canvas threadpool, which a thread cannot be force-killed
   out of): after a final grace with the task still not done, the watchdog makes a
   **synchronous best-effort** `fail(timeout)` HTTP call (plain `urllib`, since the
   async client is on the blocked loop) and then `os._exit()`s the worker so the
   container restart policy brings it back clean. Rare; only when nothing is
   killable.

### Control flow

```
_run_one(task):
    timeout = resolve_timeout(task.task_type)         # may be 0 → disabled
    wd = TaskWatchdog(timeout, grace_s) if timeout > 0 else None
    wd and wd.start()
    try:
        ... existing claim/stage/handler-under-CancelGuard/complete ...
    except ...:                                       # existing handlers
        ...
    finally:
        wd and wd.stop_and_join()                     # cancel on normal finish
        if wd and wd.fired:                           # timeout won the race
            await client.fail(task.id, f"timeout: exceeded {timeout}s")
```

Wall-clock from handler start, `monotonic()`-based, independent of heartbeats.
The existing cooperative `CancelGuard` and heartbeat paths are unchanged; the
watchdog is additive.

### Edge cases

- Task finishes before the deadline → `finally` stops/joins the watchdog; no kill.
- One task at a time → exactly one watchdog; no cross-task interference.
- Clean worker shutdown → watchdog is a daemon thread and is stopped/joined.
- `fail()` after a kill: when children are killed and the loop unblocks, the normal
  async `fail` path runs. Only the in-process-exit branch needs the sync `fail`.
- Disabled (`<= 0`) → no watchdog created.
- Double-report guard: if both the handler-exception path and the `timed_out` flag
  would call `fail`, the `timed_out` flag wins and is reported once.

## Testing

- Fake long handler (sleeps past a tiny test timeout) → asserts terminate +
  `client.fail("timeout…")` within `timeout + grace`, via `FakeBackendClient`.
- Subprocess case: handler spawns `sleep 600` → asserts the child is killed and the
  task is failed.
- Blocked-loop case: handler does a synchronous `time.sleep` (blocks the loop) →
  asserts the watchdog thread still fires and acts (validates the core fix).
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

## Open questions

None. (Grace period default 15s, env-var format `default=…,type=…`, and
exit-as-last-resort for in-process work were all confirmed during design.)
