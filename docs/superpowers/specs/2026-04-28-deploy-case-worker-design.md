# Deploy Case Worker — Design Spec

**Date:** 2026-04-28  
**Status:** Approved  
**Scope:** Add `DEPLOY_CASE` task type to the SDK, wire the SynPusher backend to create tasks on deploy, scaffold the `syngar-ml-assetbundle-builder` Python worker that claims and processes them.

---

## Problem

The SynPusher "Deploy Case" button (`POST /api/v1/deploy/{case_id}`) currently exports the model collection and sets a status flag — but nothing picks up the actual Unity AssetBundle build. The assetbundle-builder project is a Unity C# container with a `build-bundle.sh` CLI entry point; it needs a Python SDK worker wrapper so it can participate in the same task queue as colmap-splat, Neural-Canvas, and Blender-CLI.

This spec covers the scaffolding work only. The integration of `build-bundle.sh` into the handler is a separate session.

---

## Architecture

```
Frontend (SetupMenu)
  └─ POST /api/v1/deploy/{case_id}
       │
       ▼
SynPusher backend (model_collection.py)
  ├─ export_case_model_collection(case_id)     ← unchanged
  ├─ Case.update(content_integration_status=IN_QUEUE)  ← unchanged
  └─ Task.create_or_reject(DEPLOY_CASE, case_id)       ← NEW
       │
       ▼
task-worker-api queue
       │
       ▼
assetbundle-builder worker (worker/src/sdk_worker.py)
  └─ handlers/deploy_case.py
       ├─ GET /api/v1/cases/{case_id}  → resolve content path
       ├─ Check folder empty/missing  → TaskParamsError if so
       └─ Return {} stub (build logic: future session)
```

---

## Section 1: SDK changes (`task-worker-api` → v0.6.0)

### `src/task_worker_api/enums.py`
Add to `TaskType`:
```python
DEPLOY_CASE = "deploy_case"
```

### `src/task_worker_api/schemas/deploy_case.py` (new file)
```python
from .base import TaskParamsBase

class DeployCaseParams(TaskParamsBase):
    build_target: str = "Android"
```

`case_id` is already on `ClaimedTask` and is not repeated in params. `build_target` is the only thing the backend can't derive from context.

### `src/task_worker_api/schemas/__init__.py`
Register in `TASK_PARAMS_SCHEMAS`:
```python
TaskType.DEPLOY_CASE: DeployCaseParams,
```

### Artifact + release
- Re-run `tools/gen_typescript.py` to update `artifacts/task-worker-types/index.ts`
- Bump `pyproject.toml` to `0.6.0`
- Add CHANGELOG entry
- Merge to main → CI publishes wheel

---

## Section 2: SynPusher backend changes

### `routes/cases/model_collection.py`

Extend `deploy_case()` to create the task after the export:

```python
async def deploy_case(case_id: int):
    export_result = await export_case_model_collection(case_id)
    await Case.filter(id=case_id).update(
        content_integration_status=BuildStatus.IN_QUEUE.value
    )
    await Task.create_or_reject(
        case_id=case_id,
        task_type=TaskType.DEPLOY_CASE,
        item_key="",
        params={"build_target": "Android"},
    )
    return JSONResponse(content={
        "success": True,
        "model_collection": export_result,
        "status": await fetch_case_content_integration_status(case_id),
    })
```

`create_or_reject` silently rejects if an active DEPLOY_CASE task already exists for this case — double-clicking the button is safe.

### `database/task_models.py`

Add DEPLOY_CASE to both concurrency maps:

```python
MAX_CONCURRENT[TaskType.DEPLOY_CASE] = 1
STALE_THRESHOLDS[TaskType.DEPLOY_CASE] = timedelta(minutes=30)
```

### Dependency pin

Update `task-worker-api` pin in the backend's requirements to `>= 0.6.0`.

---

## Section 3: Worker scaffold (`syngar-ml-assetbundle-builder/worker/`)

### Directory layout

```
worker/
├── pyproject.toml
├── Dockerfile
├── .env.example
└── src/
    ├── sdk_worker.py
    └── handlers/
        └── deploy_case.py
```

The `worker/Dockerfile` is a lightweight `python:3.12-slim` image. The existing repo-root `Dockerfile` (Unity build container) is untouched and will be integrated in a future session.

### Environment variables

| Var | Purpose |
|---|---|
| `SYNPUSHER_URL` | Backend API root e.g. `http://synpusher-backend:5000/api/v1` |
| `WORKER_API_KEY` | Auth token |
| `WORKER_ID` | Unique worker name e.g. `assetbundle-builder-1` |
| `WORKER_WORKDIR` | Scratch dir, default `/tmp/assetbundle-worker` |
| `SHARED_VOLUME_PATH` | Mount point for shared storage |

### `worker/src/sdk_worker.py`

Follows colmap-splat pattern exactly — reads env vars, constructs `Worker`, calls `asyncio.run(worker.run_forever())`.

### `worker/src/handlers/deploy_case.py`

Handler logic for this session:

1. Call `GET /api/v1/cases/{case_id}` on the backend (using `SYNPUSHER_URL` + `WORKER_API_KEY`) to get the case's content folder path relative to the shared volume.
2. Resolve to absolute path: `Path(shared_volume_path) / relative_path`.
3. If the resolved path does not exist or contains no files → raise `TaskParamsError("case folder is empty or missing")`. This marks the task as FAILED with a clear message.
4. Otherwise → log `"[STUB] build would run here"` and return `{}`.

The `TaskParamsError` propagation is handled by the SDK — the worker loop catches it, marks the task FAILED, and continues polling.

---

## Section 4: Fleet registry update

Add a row to `docs/fleet/workers.json` for `assetbundle-builder`:

```json
{
  "id": "assetbundle-builder",
  "repo": "SyngularXR/syngar-ml-assetbundle-builder",
  "task_types": ["deploy_case"],
  "sdk_version": ">=0.6.0",
  "status": "active"
}
```

---

## Test scenario

**Goal:** Confirm end-to-end task creation and pickup with a graceful empty-folder failure.

1. Start the worker pointing at the local SynPusher backend.
2. Click "Deploy Case" in the SynPusher UI for any case.
3. Worker claims the DEPLOY_CASE task.
4. Worker calls back to get the case content path.
5. Path resolves to an empty test folder.
6. Worker raises `TaskParamsError("case folder is empty or missing")`.
7. Task row in SynPusher shows status FAILED with that error message.

Success criteria: task transitions `PENDING → CLAIMED → FAILED` with the error visible in the backend task record.

---

## Out of scope

- Integrating `build-bundle.sh` into the handler (next session)
- Connecting the worker Dockerfile to the existing Unity build container
- Frontend status polling / progress display for DEPLOY_CASE tasks
