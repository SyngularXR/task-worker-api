# task-worker-api

Shared contract + worker SDK for the [SynPusher](https://github.com/SyngularXR/SynPusher-Vue) task queue.

**Build a new worker in ~30 minutes →** [docs/adding-a-worker.md](docs/adding-a-worker.md)

## What this is

One Python package consumed by three kinds of callers:

- **SynPusher backend** imports the Pydantic schemas to validate task params on `POST /tasks/{type}`.
- **Workers** (Blender-CLI, colmap-splat, Neural-Canvas, yours next) use the `Worker` class for the whole claim + heartbeat + cancel + file-transfer + publish protocol. Typical worker `main.py` is ~30 lines.
- **Frontend** consumes the generated TypeScript types in [`artifacts/task-worker-types/`](artifacts/task-worker-types/) to get end-to-end type safety on the wire.

## Install

```
pip install task-worker-api @ git+https://github.com/SyngularXR/task-worker-api.git@v0.6.0
```

Latest release: [v0.6.0](https://github.com/SyngularXR/task-worker-api/releases/tag/v0.6.0). See [CHANGELOG.md](CHANGELOG.md).

## Quick look at what a worker looks like

```python
import asyncio, os
from task_worker_api import TaskType, Worker

from my_worker.handlers import my_thing

async def main():
    worker = Worker(
        backend_url=os.environ["SYNPUSHER_URL"],
        api_key=os.environ["WORKER_API_KEY"],
        worker_id="my-worker-1",
        handlers={TaskType.MY_THING: my_thing.run},
    )
    await worker.run_forever()

asyncio.run(main())
```

And a handler:

```python
from task_worker_api import TaskContext
from task_worker_api.schemas import MyThingParams

async def run(ctx: TaskContext, params: MyThingParams) -> dict:
    input_file = ctx.files.primary_path   # staged for you
    await ctx.progress.update("working", 0, 1)
    result = do_the_thing(input_file, params)
    await ctx.progress.update("done", 1, 1)
    return {"output_files": {"result": "out.bin"}}
```

That's the whole public surface. The SDK handles the HTTP protocol, heartbeat, cancel polling, and file staging.

## What's in each module

| Module | What it owns |
|---|---|
| `task_worker_api.enums` | `TaskType`, `TaskStatus` — single source of truth for both backend + workers |
| `task_worker_api.schemas` | Pydantic params schemas per task type, `TASK_PARAMS_SCHEMAS` registry, `extra="forbid"` for drift prevention |
| `task_worker_api.client` | `BackendClient` — async HTTP with retry-on-transient |
| `task_worker_api.worker` | `Worker.run_forever()`, `run_hybrid()` for FastAPI coexistence |
| `task_worker_api.context` | `TaskContext` (what handlers receive), `ClaimedTask` |
| `task_worker_api.cancel` | `CancelGuard` async context manager — three documented patterns |
| `task_worker_api.progress` | `ProgressReporter` — handler-facing API + background heartbeat |
| `task_worker_api.files` | `prepare_inputs` / `upload_outputs` — local vs remote auto-detect |
| `task_worker_api.testing` | `FakeBackendClient` for handler unit tests |

## Three workers already using it

| Repo | Handles | Cancel style |
|---|---|---|
| [Blender-CLI](https://github.com/SyngularXR/Blender-CLI) | `detect_cut_planes`, `model_initializing` | Subprocess via `blender-pipe` |
| [colmap-splat](https://github.com/SyngularXR/colmap-splat) | `gs_build` | Subprocess wrapping `run.sh` |
| [Neural-Canvas](https://github.com/SyngularXR/Neural-Canvas) | `segmentation` (hybrid — FastAPI + worker) | Threadpool GPU |

Each worker's entry point is ~30-50 lines of glue. All protocol code lives here.

## Further reading

- **[docs/adding-a-worker.md](docs/adding-a-worker.md)** — Full walkthrough for building a new worker.
- **[Design spec](https://github.com/SyngularXR/SynPusher-Vue/blob/main/docs/specs/2026-04-22-unified-task-queue-api-contract-design.md)** — Why this package exists; the architecture; the migration from per-repo protocol copies.
- **[Backend guide](https://github.com/SyngularXR/SynPusher-Vue/blob/main/docs/guide/task-system.md)** — What the SynPusher backend does with the claim queue.
