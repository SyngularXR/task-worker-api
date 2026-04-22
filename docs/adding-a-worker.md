# Adding a new worker to SynPusher

This guide walks you through building a worker that claims tasks from
SynPusher and runs them. The goal is a running worker in ~30 minutes.

Use this when you have some interesting piece of work — a new ML model,
a CAD export, a geometry processor, anything — and you want it to be a
first-class citizen of the task queue. Your worker claims jobs, reports
progress, supports cancel, and publishes outputs, all with about 50
lines of code. The SDK owns the boring stuff.

---

## What you're getting

- A `Worker` class that claims tasks from SynPusher's `/tasks/next`
  endpoint, pushes heartbeats while your handler runs, and handles
  cancel / file transfer / error reporting automatically.
- Typed Pydantic param schemas so your handler gets validated input,
  not a stringly-typed dict.
- A drop-in testing harness (`FakeBackendClient`) so you can test
  the full claim → run → complete loop without a real backend.
- End-state: a `main.py` that looks like this, plus one handler file:

```python
import asyncio
from task_worker_api import TaskType, Worker

from my_worker.handlers import my_cool_thing

async def main():
    worker = Worker(
        backend_url=os.environ["SYNPUSHER_URL"],
        api_key=os.environ["WORKER_API_KEY"],
        worker_id="my-cool-worker-1",
        handlers={TaskType.MY_COOL_THING: my_cool_thing.run},
    )
    await worker.run_forever()

asyncio.run(main())
```

---

## The 5-minute hello-world

Say you want to build a worker that renders a point cloud to a preview
image. Call it `pointcloud_preview`.

### 1. Pick a task type name

Task types are strings like `render`, `gs_build`, `detect_cut_planes`.
Pick a snake_case name that describes the work. Max 20 chars.

For this example: `pointcloud_preview`.

### 2. Register the type in the SDK

Open a PR against [`task-worker-api`](https://github.com/SyngularXR/task-worker-api):

**`src/task_worker_api/enums.py`**
```python
class TaskType(str, Enum):
    RENDER = "render"
    GS_BUILD = "gs_build"
    # … existing types …
    POINTCLOUD_PREVIEW = "pointcloud_preview"  # new
```

**`src/task_worker_api/schemas/pointcloud_preview.py`** (new file)
```python
from pydantic import Field
from ._base import TaskParamsBase

class PointcloudPreviewParams(TaskParamsBase):
    input_path: str = Field(..., description="Path to .ply on shared volume.")
    width: int = Field(default=512, ge=16, le=4096)
    height: int = Field(default=512, ge=16, le=4096)
    viewpoint: str = Field(default="front", pattern="^(front|side|top|iso)$")
```

**`src/task_worker_api/schemas/__init__.py`**
```python
from .pointcloud_preview import PointcloudPreviewParams

TASK_PARAMS_SCHEMAS = {
    # … existing entries …
    TaskType.POINTCLOUD_PREVIEW: PointcloudPreviewParams,
}
```

The `extra="forbid"` on `TaskParamsBase` means the backend will 422
any `POST /tasks/pointcloud_preview` with an unknown field — the
exact bug class that triggered this whole architecture.

Tag a release (`git tag v0.4.0 && git push --tags`). That's it — SDK
change done.

### 3. Register concurrency + stale threshold on the backend

The backend doesn't strictly need to know about your new task type
(DEFAULT_MAX_CONCURRENT=1, DEFAULT_STALE_THRESHOLD=15min kick in via
`.get(tt, DEFAULT)` fallbacks), but you probably want to tune them:

**`SynPusher-Vue/services/backend/src/database/task_models.py`**
```python
MAX_CONCURRENT = {
    # … existing tunings …
    TaskType.POINTCLOUD_PREVIEW: 2,    # GPU-bound, bench says 2 fits
}

STALE_THRESHOLDS = {
    # … existing tunings …
    TaskType.POINTCLOUD_PREVIEW: timedelta(minutes=5),
}
```

Bump the `task-worker-api` pin in `services/backend/requirements.txt`
to your new release. Deploy backend.

### 4. Build the worker

Create a new repo (or folder in an existing one):

```
my-pointcloud-worker/
├── pyproject.toml         # or requirements.txt
├── Dockerfile
└── src/
    ├── sdk_worker.py       # ~30 lines
    └── handlers/
        ├── __init__.py
        └── pointcloud_preview.py
```

**`src/handlers/pointcloud_preview.py`**
```python
import asyncio
from task_worker_api import TaskContext
from task_worker_api.schemas import PointcloudPreviewParams

async def run(
    ctx: TaskContext, params: PointcloudPreviewParams,
) -> dict:
    """Render `input_path` to an image and return the output filename."""
    input_ply = str(ctx.files.primary_path)           # staged for you
    out_png = ctx.files.output_dir / "preview.png"

    await ctx.progress.update("loading", 0, 3)
    cloud = await asyncio.to_thread(load_ply, input_ply)

    await ctx.progress.update("rendering", 1, 3)
    image = await asyncio.to_thread(
        render, cloud, params.width, params.height, params.viewpoint,
    )

    await ctx.progress.update("saving", 2, 3)
    await asyncio.to_thread(save_png, image, out_png)

    await ctx.progress.update("done", 3, 3)
    return {"output_files": {"preview": "preview.png"}}
```

**`src/sdk_worker.py`**
```python
import asyncio, logging, os, sys
from task_worker_api import TaskType, Worker
from src.handlers import pointcloud_preview

def main() -> int:
    logging.basicConfig(level=logging.INFO)
    worker = Worker(
        backend_url=os.environ["SYNPUSHER_URL"].rstrip("/"),
        api_key=os.environ["WORKER_API_KEY"],
        worker_id=os.environ.get("WORKER_ID", "pointcloud-worker-1"),
        shared_volume_path=os.environ.get("SHARED_VOLUME_PATH"),
        handlers={TaskType.POINTCLOUD_PREVIEW: pointcloud_preview.run},
    )
    try:
        asyncio.run(worker.run_forever())
    except KeyboardInterrupt:
        pass
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

**`requirements.txt`**
```
task-worker-api @ git+https://github.com/SyngularXR/task-worker-api.git@v0.4.0
# + your domain deps (open3d, trimesh, whatever)
```

That's the whole worker. No HTTP client, no heartbeat loop, no cancel
plumbing, no file staging — the SDK owns all that.

### 5. Authorize the worker key on the backend

Every worker authenticates with an API key that's scoped to a set of
allowed task types. Add to the deployment env:

```
WORKER_API_KEYS="…existing…;pointcloud-worker-key:pointcloud_preview"
```

And on the worker side:

```
SYNPUSHER_URL=http://nexus-core:5000/api/v1
WORKER_API_KEY=pointcloud-worker-key
WORKER_ID=pointcloud-worker-1
ENABLE_TASK_WORKER=true
SHARED_VOLUME_PATH=/app/shared      # if scenes live on a shared mount
```

Deploy. Your worker shows up in [`/admin/tasks`](http://localhost:5000/admin/tasks)
as idle, and anything that POSTs to `/api/v1/tasks/pointcloud_preview`
gets claimed by it.

---

## The handler contract in detail

Every handler is:

```python
async def run(ctx: TaskContext, params: TypedParams) -> dict:
    ...
    return result_dict
```

### `ctx: TaskContext`

| Attribute | What it is |
|---|---|
| `ctx.task.id` | int — the Task row id |
| `ctx.task.case_id` | int | None — SynPusher case this task belongs to |
| `ctx.task.item_key` | str — opaque scope key, often the scene/mesh name |
| `ctx.task.params` | the raw dict (same info as `params`, for escape-hatch cases) |
| `ctx.files.primary_path` | Path — your main input file, already copied/downloaded |
| `ctx.files.input_dir` | Path — where all input files were staged |
| `ctx.files.output_dir` | Path — write outputs here; the SDK publishes them |
| `ctx.files.all_paths` | dict[str, Path] — logical-name → staged path (multi-input tasks) |
| `ctx.progress.update(stage, current, total)` | coroutine — push a progress update to the backend |
| `ctx.progress.is_cancelled` | bool — true when backend has flipped the row to CANCELLED |
| `ctx.progress.raise_if_cancelled()` | raise `TaskCancelled` if cancelled; no-op otherwise |

### `params: TypedParams`

Whatever Pydantic model you registered for your task type. Fields are
validated by the backend on enqueue (`extra="forbid"`), and again by
the SDK on claim (defense in depth), so by the time your handler runs
you know:

- Required fields are present (never `None`).
- Numeric constraints hold (your `ge=0` means `params.iterations` is
  never negative).
- No surprise fields showed up.

### Return value

A dict for the `PUT /tasks/{id}/complete` payload. Shape is your
choice, but two conventions:

- **`output_files`**: `{logical_key: filename}`. The SDK publishes
  these — local mode copies to `shared_volume_path/<task_id>/`; remote
  mode PUTs each via the file endpoints.
- **Everything else**: lives on `task.result` verbatim, accessible to
  the frontend or task-result-mirror hooks. Keep it small — JSONB
  column, but don't stuff megabytes of floats in there.

### Errors

Just raise. The SDK catches and reports:

- `TaskCancelled` → backend marks CANCELLED (cooperative cancel path).
- `TaskParamsError` → backend marks FAILED, error says "invalid params".
- Anything else → backend marks FAILED with the full traceback as
  `task.error`, so operators can diagnose from `/admin/tasks`.

Don't swallow errors. Raising is the right signal.

---

## Picking a cancel pattern

When the backend flips a task to CANCELLED (user hit cancel, or admin
dashboard clicked stop), the SDK's `CancelGuard` polls
`/tasks/{id}/cancel-status` every 2 seconds. It sets
`ctx.progress.is_cancelled = True` and, if your handler awaits
anywhere in the hot loop, `TaskCancelled` is raised at the next await.

Three canonical handler shapes, pick yours:

### Pattern 1 — Pure async handler

Best for: I/O-bound handlers (HTTP calls, async DB, async file ops).

```python
async def run(ctx, params):
    async with httpx.AsyncClient() as http:
        for chunk_url in params.chunks:
            ctx.progress.raise_if_cancelled()
            chunk = await http.get(chunk_url)       # cancel lands here
            await asyncio.to_thread(process, chunk)
    return {...}
```

### Pattern 2 — Subprocess handler

Best for: shelling out to CLIs, long-running processes, bpy/Blender
code that segfaults on threadpools (Pattern 2 is specifically how
Blender-CLI and colmap-splat do it).

```python
async def run(ctx, params):
    proc = await asyncio.create_subprocess_exec(
        "my-cli-tool", "--input", str(ctx.files.primary_path),
        stdout=asyncio.subprocess.PIPE,
    )

    # Bridge: if SDK sees cancel, SIGTERM the subprocess.
    async def _kill_on_cancel():
        while proc.returncode is None:
            if ctx.progress.is_cancelled:
                proc.terminate()
                return
            await asyncio.sleep(0.5)

    killer = asyncio.create_task(_kill_on_cancel())
    try:
        stdout, _ = await proc.communicate()  # cancel lands here
    finally:
        killer.cancel()

    ctx.progress.raise_if_cancelled()          # translate to TaskCancelled
    return json.loads(stdout)
```

The SDK has this pattern baked into its own `CancelGuard` docs (see
`src/task_worker_api/cancel.py`). Copy-paste the shape.

### Pattern 3 — Threadpool handler (GPU / long sync work)

Best for: ML inference, tight numpy loops, anything where you can't
yield to the event loop mid-work. Neural-Canvas's segmentation handler
uses this.

```python
async def run(ctx, params):
    cancel_event = threading.Event()

    # Bridge: SDK cancel → threading.Event for the synchronous inner loop.
    async def _cancel_bridge():
        while not cancel_event.is_set():
            if ctx.progress.is_cancelled:
                cancel_event.set()
                return
            await asyncio.sleep(0.5)

    bridge = asyncio.create_task(_cancel_bridge())
    try:
        return await asyncio.to_thread(
            run_inference,                    # sync, CPU/GPU-heavy
            params, cancel_event,             # inner loop checks event
        )
    finally:
        bridge.cancel()
```

Inside `run_inference`, call `raise_if_cancelled(cancel_event, "slice N")`
between work units. The inner loop raises `TaskCancelled`; the SDK
catches it.

### Timing caveat

Cancel visibility is bounded by the SDK's `cancel_poll_interval_s`
(default 2 s) + one HTTP round-trip to `/tasks/{id}/cancel-status`.
A C extension that holds the GIL and doesn't yield won't see cancel
until it returns. This is a Python limitation, not ours — if you need
sub-second cancel in a C extension, either break the work into smaller
batches with awaits between them, or run the extension in a
subprocess you can SIGTERM.

---

## File transfer — local vs remote

The SDK auto-detects based on what's in `task.params`:

### Local mode (shared volume)

`task.params.input_path = "/shared/cases/42/scene.ply"`

The SDK copies the file into `ctx.files.input_dir` so your handler
can write to it safely without mutating the shared source. Your
`output_files` get copied back to `shared_volume_path/<task_id>/`.

This is the common case for workers running in the same Docker Compose
or Kubernetes namespace as SynPusher — everyone mounts the same
shared volume.

### Remote mode (HTTP transfer)

`task.params.input_files = {"mesh": "scene.ply", "texture": "scene.png"}`

The SDK calls `GET /tasks/{id}/files/{filename}` for each entry, streams
to disk, and presents them in `ctx.files.all_paths`. Outputs get PUT
back the same way.

Use this when the worker is in a different network (Neural-Canvas
running on a GPU box across the WAN, say).

### Neither (compute-only)

If your task doesn't need any files (pure math on `params`), just
don't set either key. `ctx.files.primary_path` will still be defined
(points at the empty input dir as a sentinel), but you can ignore it.

---

## Testing

The SDK ships a `FakeBackendClient` in `task_worker_api.testing`:

```python
import pytest
from task_worker_api import TaskType, Worker
from task_worker_api.testing import FakeBackendClient

from my_worker.handlers import pointcloud_preview

@pytest.mark.asyncio
async def test_happy_path(tmp_path):
    ply = tmp_path / "input.ply"
    ply.write_bytes(b"ply\nformat ascii 1.0\nend_header\n")

    fake = FakeBackendClient()
    fake.queue_task(
        task_type=TaskType.POINTCLOUD_PREVIEW,
        params={"input_path": str(ply), "width": 256, "height": 256},
    )

    worker = Worker(
        backend_url="http://fake/api/v1", api_key="k", worker_id="t",
        handlers={TaskType.POINTCLOUD_PREVIEW: pointcloud_preview.run},
        work_dir=str(tmp_path / "work"),
        client=fake,
    )
    await worker.run_one()                     # process exactly one task

    assert fake.failed_tasks == []
    assert fake.completed_tasks[0]["result"]["output_files"]["preview"] == "preview.png"
```

This covers the full loop: schema validation, file staging, handler
execution, completion reporting, heartbeat calls. No real HTTP, no
real backend. Run it in normal `pytest`, no docker-compose needed.

For integration testing against a real backend, `task-worker-api`'s
own repo has a docker-compose fixture at `tests/integration/` you
can adapt.

---

## Hybrid workers (FastAPI server + task worker on one process)

If your worker is also serving HTTP endpoints (Neural-Canvas is the
canonical example — it serves `/segment_unified`, `/volume_rmar`, etc.
AND claims segmentation tasks), use `run_hybrid`:

```python
import uvicorn
from fastapi import FastAPI
from task_worker_api import TaskType, Worker, run_hybrid

from my_app.fastapi_app import create_app
from my_worker.handlers import pointcloud_preview

async def main():
    app = create_app()
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=8000))
    worker = Worker(
        backend_url=...,
        api_key=...,
        handlers={TaskType.POINTCLOUD_PREVIEW: pointcloud_preview.run},
    )
    await run_hybrid(server.serve(), worker)
```

`run_hybrid` launches both concurrently via `asyncio.TaskGroup`. If
either exits (uvicorn got SIGTERM, worker failed), the other cancels
cleanly.

---

## Deployment checklist

Before you cut a release:

- [ ] Dockerfile installs `task-worker-api` at a pinned version
      (`git+https://github.com/SyngularXR/task-worker-api.git@v0.X.Y`).
- [ ] `ENABLE_TASK_WORKER=true` in the container env (or gate on it
      in your `sdk_worker.py` like the existing workers).
- [ ] `SYNPUSHER_URL`, `WORKER_API_KEY`, `WORKER_ID` env vars set.
- [ ] `SHARED_VOLUME_PATH` set if you're in local mode (mounted to
      the same path as `nexus-core`'s `/app/shared`).
- [ ] Worker's API key added to `WORKER_API_KEYS` on nexus-core's env,
      scoped to your task type.
- [ ] `MAX_CONCURRENT` + `STALE_THRESHOLDS` tuned on the backend
      (optional — defaults kick in if you skip).
- [ ] Frontend (or whatever calls `POST /tasks/{your_type}`) is aware
      of the new type and its params.
- [ ] Smoke test: submit one task, watch the admin dashboard, confirm
      it transitions pending → claimed → in_progress → completed.
- [ ] Cancel test: submit a long task, hit cancel from the dashboard,
      confirm the row goes to CANCELLED and your subprocess / thread
      actually stops.

---

## The four things the SDK handles so you don't have to

This list exists so you know what to *not* reimplement:

1. **Claim loop** — `GET /tasks/next?types=X&worker_id=Y` with the
   right auth header, retry on transient errors, back off when queue
   is empty.
2. **Heartbeat** — `PUT /tasks/{id}/progress` every 10 s while your
   handler is running, so the sweeper doesn't mark it stale.
3. **Cancel polling** — `GET /tasks/{id}/cancel-status` every 2 s,
   setting `is_cancelled` flag so your handler can bail.
4. **File transfer** — local shared-volume copy OR remote HTTP
   streaming of inputs and outputs.

If you find yourself writing any of these by hand, stop — the SDK
has a bug or a missing feature. File an issue at
[SyngularXR/task-worker-api/issues](https://github.com/SyngularXR/task-worker-api/issues)
and we'll fix it there, once, for everyone.

---

## Worked examples in production

Look at these for reference when building yours:

| Repo | Task types | Cancel pattern | Notes |
|---|---|---|---|
| [Blender-CLI](https://github.com/SyngularXR/Blender-CLI) | `detect_cut_planes`, `model_initializing` | Subprocess (Pattern 2) | Shells out to `blender-pipe` because bpy segfaults on threadpools. Minimal main.py. |
| [colmap-splat](https://github.com/SyngularXR/colmap-splat) | `gs_build` | Subprocess (Pattern 2) | Wraps run.sh + tails status.json + stall detection. Custom cancel_then_terminate watcher. |
| [Neural-Canvas](https://github.com/SyngularXR/Neural-Canvas) | `segmentation` | Threadpool (Pattern 3) | Hybrid mode: FastAPI app + Worker on one loop via `run_hybrid`. |

---

## Going further

- **Task dependencies** — if your task should only run after another
  task completes (e.g., `gs_build` after `render`), set
  `depends_on=parent_task_id` when enqueueing. The SDK claim filter
  skips tasks whose parent isn't COMPLETED.
- **Multi-target workers** — `Worker` currently binds to one backend
  URL. If you need to claim from multiple SynPusher instances,
  construct N Workers and run them in an `asyncio.TaskGroup`.
- **Custom retry / backoff** — override `BackendClient` and pass your
  instance to `Worker(client=your_client)`.

---

## Questions?

- Design spec: [`SynPusher-Vue/docs/specs/2026-04-22-unified-task-queue-api-contract-design.md`](https://github.com/SyngularXR/SynPusher-Vue/blob/main/docs/specs/2026-04-22-unified-task-queue-api-contract-design.md) — the full architecture
- Task system guide: [`SynPusher-Vue/docs/guide/task-system.md`](https://github.com/SyngularXR/SynPusher-Vue/blob/main/docs/guide/task-system.md) — what the backend does
- Open issues on [SyngularXR/task-worker-api](https://github.com/SyngularXR/task-worker-api/issues)

Build something cool.
