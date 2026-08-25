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

## 9. Terminal report idempotency (v0.15.0+)

A worker's last act on a task is one terminal report — `PUT /tasks/{id}/complete` or `PUT /tasks/{id}/fail`. Two things about that report are now contractual:

**Every terminal report carries an `Idempotency-Key` header.** The value is `task-<id>-<complete|fail>-<uuid4 hex>`, minted once per attempt. All deliveries of one outcome share it: the client's own retry loop, the watchdog thread's last-resort sync `fail`, and any re-send from the ledger below. A *different* attempt on the same task (the backend re-queued it) mints a fresh key, so a genuine second attempt is never mistaken for a duplicate of the first.

The header is additive and one-directional. Nexus Core is free to ignore it — its terminal transitions are already guarded server-side, so a duplicate `/complete` on a terminal row is dropped and behaviour is unchanged. When the backend does want to dedupe (post-complete hooks, cascade side effects), the key is the name to dedupe on; no fleet-wide SDK upgrade is needed first.

**A report the backend never confirmed is kept and re-sent.** When `complete`/`fail` outlives the client's retry budget (backend down longer than ~60s), the worker holds the outcome in a bounded in-memory ledger (`task_worker_api.reports`) and:

- re-sends it on the next poll cycle whose claim **reached the backend and came back empty**. Both halves matter. An empty queue is the only evidence a worker has that the task isn't already re-queued for a fresh attempt: re-sending a held report onto a `pending` row terminalizes it on the earlier attempt's output and silently skips the re-run the backend asked for — the same wrong answer the re-delivery rule below refuses to give, so the flush must not give it either. A claim that hands back some *other* task proves nothing about ours, which is why a busy cycle does not flush. And a *failing* claim means the backend is down, so flushing there would fight the claim backoff during an outage. The cost of the gate: a worker whose queue never drains holds its unconfirmed reports, possibly until the ledger evicts them (logged at ERROR) and the task is swept and recomputed — the pre-ledger behaviour, and the safe direction to be wrong in;
- stops re-sending, and drops the outcome, on a permanent 4xx (the ownership check, once the sweeper hands the task elsewhere) — whoever owns the task now reports it. A *transient* 4xx (408/429) is not a refusal and stays queued;
- **drops** it the moment the same task is delivered to this worker again, and runs the handler.

That last rule is the load-bearing one, and it is deliberately conservative. A re-delivery is a **new attempt**: `GET /tasks/next` returns no attempt or lease identity (see `ClaimedTask`), so a worker cannot tell "the task whose report I lost came back" from "the backend legitimately re-queued this task for a genuine re-run" — including the case where the completion *did* commit and only its response was lost. Answering a re-delivery from the ledger would report a stale result and silently skip the work that was asked for, so the SDK never does. Re-enabling that shortcut is a protocol change, not an SDK change: it needs either an attempt/lease token in the claim envelope that a held report can be matched against, or an explicit Nexus Core guarantee that a re-delivery implies the earlier terminal report never committed.

The ledger is per-process and in-memory, and bounded by both entry count and total payload bytes (a completion payload is an arbitrary handler result, so counting entries alone bounds nothing). Reports still unconfirmed when the worker stops are logged at ERROR with their task ids — those outcomes are lost and their tasks will be swept as stale.

**Nothing to migrate.** The SDK checks whether the client's `complete`/`fail` declares `idempotency_key` (or takes `**kwargs`) and only passes it if so, so a `BackendClient` subclass or test double written against `complete(task_id, result)` / `fail(task_id, error)` keeps working — it just reports without the dedupe name, and the SDK logs one WARNING naming the override. Adding `*, idempotency_key: Optional[str] = None` and forwarding it is what buys the header. The SDK's own `FakeBackendClient` records the key on a separate `idempotency_keys` list — read it with `keys_for(task_id)` to assert a re-send reused the original. `completed_tasks` / `failed_tasks` records are deliberately unchanged, since worker suites across the fleet compare them whole.
