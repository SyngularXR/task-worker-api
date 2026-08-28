# Fleet-wide conventions

These are the contracts every `task-worker-api` consumer is expected to honor. They exist so that fleet-wide changes (SDK upgrades, new env vars, new mount paths) are mechanical rather than archaeological.

If you're adding a new worker, follow these conventions. If you're updating an existing worker, the deviations from these conventions are the most likely source of subtle bugs.

## 1. SDK dep pinning

Every worker pins to a specific `task-worker-api` release tag. Two pinning styles are in use across the fleet:

| Style | Where used | Example |
|---|---|---|
| **Git ref** (`requirements.txt`) | colmap-splat | `task-worker-api @ git+https://github.com/SyngularXR/task-worker-api.git@v0.5.0` |
| **Wheel URL** (`requirements.txt` or `pyproject.toml`) | Neural-Canvas, Blender-CLI | `task-worker-api @ https://github.com/SyngularXR/task-worker-api/releases/download/v0.14.0/task_worker_api-0.14.0-py3-none-any.whl` |

**Recommended:** wheel URL when possible. Faster install (one HTTPS GET vs full git clone), no source-build step at install time, no transitive build-tooling dependency on the host. The git-ref style requires `hatchling` to be present on the host pip if `--no-build-isolation` is in play (see colmap-splat's Dockerfile fix in v0.5.0 rollout).

The current pin per worker is recorded in [`workers.json`](workers.json) under `sdk_pin`. When upgrading the SDK fleet-wide, the [SDK upgrade runbook](runbooks/sdk-upgrade.md) walks each worker's pin file in turn.

## 2. `shared_volume_path` wiring

Every worker repo's entry point must read `SHARED_VOLUME_PATH` from env and pass it to `Worker(...)`:

```python
# src/.../sdk_worker.py
worker = Worker(
    backend_url=os.environ["SYNPUSHER_URL"].rstrip("/"),
    api_key=os.environ["WORKER_API_KEY"],
    worker_id=os.environ.get("WORKER_ID", "..."),
    work_dir=os.environ.get("WORKER_WORKDIR"),
    shared_volume_path=os.environ.get("SHARED_VOLUME_PATH"),  # ← REQUIRED
    handlers={...},
)
```

**This is load-bearing for several SDK features**, including:
- Local-mode file staging under `shared/temp/{task_id}/` (since v0.4.1)
- Payload logging under `shared/_worker_payloads/{worker_id}/` (since v0.5.0)

If `shared_volume_path` is `None`, those features silently disable. The SDK does not raise — it just no-ops. The [SDK upgrade runbook](runbooks/sdk-upgrade.md) includes an audit step to verify each worker's wiring.

## 3. Required env vars

Every worker:
- `SYNPUSHER_URL` — backend base URL (e.g., `http://nexus-core:5000/api/v1`)
- `WORKER_API_KEY` — must match an entry in `WORKER_API_KEYS` on the backend
- `WORKER_ID` — must be unique per running process (matters when scaling replicas)

Workers that mount the shared volume:
- `SHARED_VOLUME_PATH=/app/shared` — wired through to `Worker(...)`

Workers in **polling mode** (everything except hybrid):
- `WORKER_TASK_TYPES` — comma-separated list, e.g., `gs_build` or `optimize,render,pipeline`
- `WORKER_WORKDIR` — ephemeral per-task scratch dir (e.g., `/tmp/colmap-splat-worker`)
- `ENABLE_TASK_WORKER=true` — dual-mode images (colmap-splat) gate worker mode on this

## 4. Payload logging contract (v0.5.0+)

Three env vars, all optional:

| Env var | Default | Purpose |
|---|---|---|
| `WORKER_PAYLOAD_LOG_ENABLED` | `true` | Master switch. `false` disables capture; cleanup of existing files still runs. |
| `WORKER_PAYLOAD_LOG_RETENTION_DAYS` | `14` | mtime-based retention. Bad/zero/negative values fall back to 14 with a WARNING. |
| `WORKER_PAYLOAD_LOG_CLEANUP_INTERVAL_S` | `3600` | Periodic cleanup loop interval. Tighter values are useful for tests. |

Files land under `/app/shared/_worker_payloads/{worker_id}/`:
- `payloads-DATE-pidPID-BOOT.jsonl` — typed envelope per claimed task, captured before schema validation.
- `raw_envelopes-DATE-pidPID-BOOT.jsonl` — raw response written when `BackendClient.claim_next` couldn't parse it (protocol-drift signal). Empty on healthy days.

Per-process file naming is what makes scaled replicas with one shared `WORKER_ID` safe — no JSONL line interleaving even on Windows Docker Desktop's WSL2 9P bind mount.

The [payload-replay runbook](runbooks/debugging-with-payload-logs.md) covers using these files to reproduce bugs.

## 5. `worker_id` path safety

The SDK sanitizes `worker_id` before using it as a path segment for the payload log directory. You should still pick a `worker_id` that's safe everywhere:

- Stick to `[A-Za-z0-9._-]`. Slashes/backslashes/colons get replaced with `_`.
- Avoid Windows reserved names (`CON`, `PRN`, `AUX`, `NUL`, `COM1`-`COM9`, `LPT1`-`LPT9`) — the SDK appends `_x` to disambiguate but the source ID is more readable if you avoid the issue.
- Make it unique per running process (relevant when scaling replicas; `docker compose --scale colmap-splat-worker=N` inherits one default ID, so consider using a hostname suffix in that mode).

## 6. Image tagging

Images live under `syngular/<repo-name>` on the registry; the deployment compose file references them via per-worker `*_TAG` env vars (`NEURAL_CANVAS_TAG`, `BLENDER_WORKER_TAG`, `COLMAP_SPLAT_WORKER_TAG`). Defaults to `:latest` for dev, ops pins specific tags for prod for reproducibility.

When CI rebuilds an image after a worker repo PR merges, it pushes both `:latest` and a dated tag (e.g., `2026.04.26-1430`).

## 7. Scaling

Three modes:

- **single**: one process per worker, no scale parameter. Default for `neural-canvas` (hybrid mode is single-process by design) and `blender-worker` (GPU bound).
- **horizontal**: `docker compose up --scale colmap-splat-worker=N` runs N replicas. The SDK's per-process payload log files (PID + 8-char boot id) and per-replica work_dir keep them from colliding. **Operators should set distinct `WORKER_ID` per replica** if they care about correlating payload logs to a specific replica; otherwise all N share one `WORKER_ID` and the log directory is one shared root with N files in it.

## 8. Failure mode contract

The SDK never raises out of payload-logging code paths (including `__init__`). Disk full, fs flap, permission errors, and serialization failures all produce one WARNING log per process lifetime; subsequent failures are silent. **Worker keeps polling and running tasks regardless** — payload logging is a debug aid, not a correctness feature.

If you implement a custom `BackendClient` subclass or override `Worker._run_one`, preserve this property: don't let logging machinery propagate exceptions out to break the polling loop.

## 9. Terminal report contract (`Idempotency-Key`)

`PUT /tasks/{id}/complete` and `PUT /tasks/{id}/fail` are the only calls the SDK retries that mutate task *state*, and the failures they retry — `ReadTimeout`, 500, 502/504 — are exactly the ones where the write may have committed and only the response was lost. Both therefore carry an `Idempotency-Key` header:

```
Idempotency-Key: task-<task_id>-<complete|fail>-<32 hex chars>
```

The key is generated once per **logical report** and reused across every retry of that report, including the watchdog's last-resort synchronous `fail` (which retries 3× against a 3s deadline on an already-wedged worker — the likeliest duplicate in the fleet). A genuinely new report — a re-claimed task's second attempt, or a `fail` following a `complete` — gets a fresh key.

**For backends:** if two terminal requests for the same task arrive with the *same* key, they are one report retried; return the first attempt's outcome rather than transitioning again. Different keys are different reports and must be handled on their own merits. Honouring the header is optional — SynPusher-Vue's guarded conditional `mark_completed`/`mark_failed` already absorbs most duplicates — but the routes that deliberately leave the row non-terminal and dispatch a finalize task (`segmentation`, `generate_synthetic`) have no status to guard on, and are where a retry currently re-runs that dispatch.

No other call carries the header: heartbeats and cancel polls are safely repeatable and have no transition to collapse.

## 10. Published outputs when an attempt fails

Publishing succeeding is not the same as the task succeeding. `upload_outputs` can deliver every file and the attempt still end terminal-**failed** — a cancel landing after the last upload (the `CancelGuard` raises when the guarded block *exits*), a watchdog deadline firing between the last upload and the terminal report, or a handler result the wire can't encode. A publish that fails partway through leaves the same residue: files 1..N-1 are already at their published names.

Since the backend only sweeps a staging dir (or accepts an output manifest) for a task it recorded as **complete**, those artifacts are orphans nothing ever reaches. `Worker._run_one` logs **one** WARNING naming them — one per failed attempt, not one per thing that noticed. `upload_outputs` records what it published and re-raises without logging; the terminal path already warns about everything the attempt published, whether the publish failed partway through or succeeded and the attempt failed afterwards.

- **Local mode** — the files this attempt staged into `${SHARED_DATA_PATH}/temp/<task_id>/`, with the staging path, so an operator can sweep them.
- **Remote mode** — the filenames uploaded to the backend. The worker protocol has no delete route, so a retry overwriting them is the only cleanup there is.

The warning is **not gated on winning the terminal report.** Terminal reporting is exactly-once via a `TerminalGuard`, and the watchdog can win it: on an in-process wedge it claims the guard and sends the timeout `fail` synchronously from its own thread. The event loop can resume afterwards (the hard exit is not instantaneous, and `on_hard_exit` is injectable), and when it does it is still the only thing that knows what this attempt staged — the watchdog's `fail` carries no such record. So `_run_one` claims the guard, then warns whichever way that claim went, and only the report itself is skipped by the loser.

### The SDK never deletes a published output

Not in either mode, and not even the ones it just wrote. `temp/<task_id>` is shared by *every* attempt of a task, and a failing attempt cannot prove the file at a published name is still the one it wrote: the watchdog reports a timeout `fail` from its own thread while the event loop is wedged, and the backend's stale-task sweeper re-queues a task whose heartbeat lapsed with no report at all. Either way a successor can already have published into that directory before this attempt resumes.

An identity check (stat the file, compare against what the attempt recorded) does not fix this. POSIX has no compare-and-unlink, so a successor that publishes between the `stat` and the `unlink` still loses a complete artifact — and the backend then reports its manifest path missing. Deleting a live attempt's outputs is a far worse outcome than an orphan an operator sweeps.

**Attempt-safe reclamation is therefore deferred** until the fleet has one of: a staging path unique per attempt that every backend consumer reads and sweeps, or a lease on the shared `temp/<task_id>` that a failing attempt can check before removing anything. Neither exists today. When one lands, the reclaim goes in behind it.

The one exception is a failing local publish's own `.part` scratch file (below) — a name no other attempt can hold.

### A local publish is a rename, not a copy over the live file

The other half of the same problem: two attempts of one task publishing at once. `upload_outputs` copies to a scratch name in the staging dir (`.<random>.part`) and then `os.replace`s it onto the published name, so that name only ever changes from one complete artifact to another. Copying straight into it truncated the destination on `open`, so a concurrent attempt's file — or the same file a consumer was mid-read on — was left spliced from two publishes while both attempts reported success.

The scratch name is generated per copy, so it is the one thing in that directory provably this attempt's: a failed or cancelled copy removes it and nothing else. Leaving it would keep the staging dir non-empty and defeat the consumer's `rmdir`.

The rename itself runs **inline on the event loop**, unlike the copy that precedes it. `asyncio.to_thread` cannot be cancelled: cancelling the await abandons the thread, which completes the rename regardless — publishing an artifact the caller then never records in `staged`, which is precisely the orphan the warning above exists to name. A same-directory rename is a metadata operation, so running it inline costs nothing and makes the publish and its record atomic with respect to cancellation. If you write your own staging path, keep that property: the copy belongs in a thread, the commit does not.

### An attempt only reports outputs it still owns

The rename settles *what* is at a published name; it does not settle *whose* it is. Two attempts of one task publish onto the same names, and the last rename wins — so attempt A can publish, attempt B (the successor the backend handed the task to after a watchdog `fail` or a stale-task sweep) can republish onto the same path, and A's manifest still points there. Byte-splicing is gone, but A would report `complete` for a manifest holding B's artifact: a complete, valid file that A's result does not describe. Consumers move each manifest path to its permanent home on a `complete`, so nothing downstream would ever find out.

So each local publish records the `(st_dev, st_ino)` of the artifact its rename committed — read from the scratch name, which no other attempt can hold — and `Worker._run_one` re-checks every published path against that record immediately before the terminal report. A path whose inode changed (or that is gone) is no longer this attempt's:

- **It is not completed on.** The attempt is reported **failed**, naming the paths a successor took over. The successor still owns the task and reports its own outcome; losing a live successor's completion to this is the intended trade against silently handing a consumer the wrong artifact.
- **It is dropped from the orphan warning.** Those files belong to a running attempt, not to this one — naming them would invite an operator to sweep a live attempt's outputs.

This is a `stat`, so it narrows rather than closes: a successor can still land between the check and the backend's write, since POSIX has no compare-and-complete. It bounds the exposure to the terminal report instead of the whole attempt. Closing it needs what safe reclamation needs — an attempt-unique staging path every consumer understands, or a lease on the shared one. Marked `ponytail:` in `Worker._run_one`.

The check runs **off the event loop and before the `TerminalGuard` is claimed.** Those stats touch the shared volume, and a stalled NFS/CIFS mount blocks them indefinitely. An attempt that claimed the guard first and then stalled here would hold the sole right to report while unable to use it: the watchdog's phase-3 claim fails, so it skips its synchronous timeout `fail` and hard-exits with the task still `in_progress` until the sweeper — the exact guarantee the watchdog exists to provide. Checking first leaves the guard free for that path, and running it in a thread keeps the heartbeat ticking (and hybrid mode's FastAPI app answering) through a merely slow mount. The check needs no claim: it is read-only, and the report it can rewrite is still decided under the claim. It is also **bounded** (30s): by then the watchdog has been stopped, so nothing else would time a hung mount out, and the heartbeat this attempt is still sending keeps the stale-task sweeper away too — an unbounded stat would park the task `in_progress` forever. A check that times out is treated like a path that can't be read: the attempt is reported failed, naming the stalled volume rather than a phantom concurrent attempt.

**Nothing about the on-volume layout has changed since v0.4.1.** Outputs sit directly under `temp/<task_id>/`, `output_files` carries their absolute paths, and a consumer that moves the artifacts out and then `rmdir`s `temp/<task_id>` non-recursively still finds it empty. Scratch files exist only for the duration of a copy.

**If you override `Worker._run_one`,** pass a list as `upload_outputs(..., staged=...)` and a dict as `owned=`; both are filled as each artifact is published. `staged` is what you log the orphans from if the attempt ends failed — do that whenever it ends failed, not only when your loop wins the terminal report. `owned` is what you pass to `_unowned_outputs()` before reporting `complete`: a non-empty return means a concurrent attempt took those names over, and you should report the failure instead and drop them from what you warn about. Call it off the loop (`await asyncio.to_thread(...)`) and *before* you claim the guard, for the reason above, and bound it — you have already stopped the watchdog by then. Like the payload logger (§8) both SDK helpers never raise — they run while a failure is already being reported, so they must not displace the real one. Don't replace either with a delete — see above.
