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

## 10. Partial-failure cleanup

Publishing succeeding is not the same as the task succeeding. `upload_outputs` can deliver every file and the attempt still end terminal-**failed** — a cancel landing after the last upload (the `CancelGuard` raises when the guarded block *exits*), a watchdog deadline firing between the last upload and the terminal report, or a handler result the wire can't encode.

Since the backend only sweeps a staging dir (or accepts an output manifest) for a task it recorded as **complete**, those artifacts would be permanent orphans. `Worker._run_one` therefore discards them *before* reporting the failure:

- **Local mode** — the files this attempt staged into `${SHARED_DATA_PATH}/temp/<task_id>/` are unlinked, and the directory goes if that empties it, so a retried task starts clean.
- **Remote mode** — the worker protocol has no delete route, so the uploads stay until a retry overwrites them; the SDK logs one WARNING naming the files for operator reconciliation.

Cleanup runs before the report because reporting the failure is what makes the task re-queueable, and a retry stages into the *same* `temp/<task_id>` path — so discarding first keeps this attempt's cleanup out of the next attempt's way.

**Ordering alone is not enough, so the local-mode reclaim proves ownership per file.** This worker is not the only thing that can hand the task to a successor: the watchdog reports a timeout `fail` from its own thread while the event loop is wedged, and the backend's stale-task sweeper re-queues a task whose heartbeat lapsed with no report at all. Either way a second attempt can already have staged its outputs into that shared directory. `upload_outputs` therefore records each staged file's identity (`st_ino`, `st_size`, `st_mtime_ns`, `st_ctime_ns`) *as the copy lands*, and the reclaim unlinks only the paths that still match, leaving anything a newer attempt owns in place (one WARNING per reclaim naming the count). Deleting a live attempt's outputs is a far worse outcome than the orphan this cleanup exists to prevent.

**A local publish is a rename, not a copy over the live file.** The other half of the same problem: two attempts of one task publishing at once. `upload_outputs` copies to a scratch name in the staging dir (`.<random>.part`) and then `os.replace`s it onto the published name, so that name only ever changes from one complete artifact to another. Copying straight into it truncated the destination on `open`, so a concurrent attempt's file — or the same file a consumer was mid-read on — was left spliced from two publishes while both attempts reported success. The rename also makes the identity record above meaningful: it is taken from a scratch path no other attempt can name, so what it describes is provably this attempt's.

The remaining window is small and one-sided: POSIX has no compare-and-unlink, so a successor that publishes between the reclaim's `stat` and its `unlink` still loses that file. It loses a *complete* artifact and the backend reports the manifest path missing — where before, the successor would carry on writing into an inode the reclaim had already unlinked and complete with a manifest pointing at nothing. Closing it entirely means giving each attempt a staging directory of its own, which changes the on-volume layout every backend consumer reads and sweeps, so it needs those consumers to land first.

**Nothing about the on-volume layout has changed since v0.4.1.** Outputs sit directly under `temp/<task_id>/`, `output_files` carries their absolute paths, and a consumer that moves the artifacts out and then `rmdir`s `temp/<task_id>` non-recursively still finds it empty. The scratch files exist only for the duration of a copy and are removed when one fails.

**If you override `Worker._run_one`,** pass a dict as `upload_outputs(..., staged=...)` and hand that same dict to `discard_published_outputs` — a reclaim with no ownership record removes nothing.

`discard_published_outputs` is exported for consumers that override `Worker._run_one`. Like the payload logger (§8) it never raises: it runs while a failure is already being reported, so a cleanup error must not displace the real one.
