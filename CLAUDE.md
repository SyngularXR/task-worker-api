# task-worker-api — repo guide for Claude

This repo is **two things**:

1. **An SDK** — the `task_worker_api` Python package. Drops ~500 lines of boilerplate (claim, heartbeat, cancel, file transfer, error handling, polling) from every worker repo that consumes the SynPusher task queue. Source under `src/task_worker_api/`.
2. **The fleet HQ** — the canonical place to look up which workers exist, where they live, what task types they handle, what env vars they need, and how to upgrade or debug them. Docs under `docs/fleet/`.

**For fleet questions**, start at [`docs/fleet/README.md`](docs/fleet/README.md). The machine-readable manifest at [`docs/fleet/workers.json`](docs/fleet/workers.json) is what backends and automation should fetch.

**For SDK-consumer questions** (how do I write a worker that consumes this), see [`docs/adding-a-worker.md`](docs/adding-a-worker.md).

## Layout at a glance

```
src/task_worker_api/        SDK package
├── worker.py               Worker.run_forever(), _run_one(), run_hybrid()
├── client.py               BackendClient — async HTTP wrapper for the worker protocol
├── context.py              ClaimedTask, FileContext, TaskContext (typed envelopes)
├── payload_log.py          PayloadLogger (v0.5.0+) — JSONL capture of claimed tasks
├── progress.py             ProgressReporter (heartbeat + progress)
├── cancel.py               CancelGuard (cooperative cancel)
├── files.py                prepare_inputs / upload_outputs (local + remote modes)
├── conventions.py          Filename conventions shared with backend
├── schemas/                TaskParamsBase + per-task-type Pydantic schemas
├── enums.py                TaskType, TaskStatus
└── errors.py               ProtocolError, TaskCancelled, TaskParamsError

docs/
├── fleet/                  Fleet HQ — workers.json, conventions, runbooks
├── adding-a-worker.md      Per-worker SDK consumer guide
└── superpowers/            specs/ + plans/ from prior development cycles

tests/                      pytest + pytest-asyncio
tools/                      gen_typescript.py — codegen for the TS artifact

artifacts/task-worker-types/  Generated TypeScript artifact (committed; drift-checked in CI)
```

## Common tasks

| If you want to... | Look at... |
|---|---|
| Add a new task_type | `enums.py` + a new file under `schemas/` + register in `schemas/__init__.py`. Drift check (`tools/gen_typescript.py`) runs in CI. |
| Add a new worker repo | `docs/adding-a-worker.md` for the SDK side, then add a row to `docs/fleet/workers.json` for the fleet side. |
| Bump the SDK release across all worker repos | `docs/fleet/runbooks/sdk-upgrade.md` |
| Reproduce a worker bug from production | `docs/fleet/runbooks/debugging-with-payload-logs.md` |
| Pull latest worker images on the deploy host | `docs/fleet/runbooks/local-testing.md` |
| Understand the payload-logging design | `docs/superpowers/specs/2026-04-26-payload-logging-design.md` |

## Conventions you'll keep tripping over if you don't read them

- **`shared_volume_path` is load-bearing.** Many SDK features (file staging, payload logging) are gated on the consumer passing `shared_volume_path=os.environ.get("SHARED_VOLUME_PATH")` to `Worker(...)`. The SDK silently disables those features when it's `None`. See `docs/fleet/conventions.md` § 2.
- **`PayloadLogger` never raises.** Every public method (including `__init__`) absorbs exceptions, logs one WARNING per process lifetime, and continues. Don't try to make it raise; that breaks the worker polling loop. See `docs/fleet/conventions.md` § 8.
- **TypeScript artifact is committed and drift-checked.** Don't hand-edit `artifacts/task-worker-types/index.ts` — regenerate via `tools/gen_typescript.py`.
- **Releases publish via CI.** Every merge to main runs the release workflow which publishes a wheel asset to GitHub Releases at the version in `pyproject.toml`. Bump version → CHANGELOG → merge → wheel appears.

## Tests

```bash
pip install -e ".[dev]"   # editable install — required, the package is shipped, not vendored
pytest -q                  # 52 tests at v0.5.0
```

Test framework: pytest + pytest-asyncio. Integration tests in `tests/test_payload_log_integration.py` use `httpx.MockTransport` (built-in, no extra dep) to exercise paths `FakeBackendClient` bypasses.

## Cross-repo coordinates (as of v0.5.0)

- **SDK release tag**: `v0.5.0` — see [`docs/fleet/workers.json`](docs/fleet/workers.json) `sdk.current_release` for the live value.
- **Workers consuming the SDK**: Neural-Canvas, Blender-CLI, colmap-splat. Status per worker in `workers.json`.
- **Deployment**: surgiclaw in `syngar-deployment-scripts`. Mounts `${SHARED_DATA_PATH}:/app/shared` for every worker.
- **Backend**: SynPusher-Vue's Nexus Core dispatches tasks. Should fetch `workers.json` to know handler routing.
