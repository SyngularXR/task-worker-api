# Deploy Case Worker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `DEPLOY_CASE` to the task-worker-api SDK, wire the SynPusher backend to enqueue a task on deploy, and scaffold the `syngar-ml-assetbundle-builder` Python worker that claims and processes those tasks.

**Architecture:** The SynPusher "Deploy Case" button already calls `POST /api/v1/deploy/{case_id}`; we extend that handler to also call `Task.create_or_reject(DEPLOY_CASE)`, passing the case content folder path in the task params. A new Python worker under `syngar-ml-assetbundle-builder/worker/` claims `DEPLOY_CASE` tasks, checks that the content folder is non-empty, and returns a stub success. The Unity build integration is a separate session.

**Tech Stack:** Python 3.10+, Pydantic v2, task-worker-api SDK, httpx, pytest, Tortoise ORM (backend), Docker.

---

## File map

| Repo | Action | Path |
|---|---|---|
| task-worker-api | modify | `src/task_worker_api/enums.py` |
| task-worker-api | create | `src/task_worker_api/schemas/deploy_case.py` |
| task-worker-api | modify | `src/task_worker_api/schemas/__init__.py` |
| task-worker-api | modify | `tests/test_schemas.py` |
| task-worker-api | regen | `artifacts/task-worker-types/index.ts` |
| task-worker-api | modify | `pyproject.toml` |
| task-worker-api | modify | `CHANGELOG.md` |
| task-worker-api | modify | `docs/fleet/workers.json` |
| SynPusher-Vue | modify | `services/backend/src/database/task_models.py` |
| SynPusher-Vue | modify | `services/backend/src/routes/cases/model_collection.py` |
| SynPusher-Vue | modify | `services/backend/requirements.txt` |
| assetbundle-builder | create | `worker/pyproject.toml` |
| assetbundle-builder | create | `worker/.env.example` |
| assetbundle-builder | create | `worker/Dockerfile` |
| assetbundle-builder | create | `worker/src/__init__.py` |
| assetbundle-builder | create | `worker/src/sdk_worker.py` |
| assetbundle-builder | create | `worker/src/handlers/__init__.py` |
| assetbundle-builder | create | `worker/src/handlers/deploy_case.py` |

---

## Task 1: Add DEPLOY_CASE enum + schema to the SDK

**Repo:** `task-worker-api` (this worktree)

**Files:**
- Modify: `src/task_worker_api/enums.py`
- Create: `src/task_worker_api/schemas/deploy_case.py`
- Modify: `src/task_worker_api/schemas/__init__.py`
- Modify: `tests/test_schemas.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_schemas.py`:

```python
from task_worker_api.schemas import TASK_PARAMS_SCHEMAS, CinematicBakingParams, DeployCaseParams


def test_deploy_case_registered():
    assert TASK_PARAMS_SCHEMAS[TaskType.DEPLOY_CASE] is DeployCaseParams


def test_deploy_case_defaults():
    obj = DeployCaseParams()
    assert obj.model_dump() == {"build_target": "Android", "content_path": None}


def test_deploy_case_roundtrip():
    obj = DeployCaseParams(build_target="iOS", content_path="/app/shared/content/abc123")
    assert obj.model_dump() == {"build_target": "iOS", "content_path": "/app/shared/content/abc123"}


def test_deploy_case_rejects_extra_field():
    with pytest.raises(Exception):
        DeployCaseParams(surprise="extra")
```

- [ ] **Step 2: Run to verify they fail**

```bash
cd P:\Project\task-worker-api\.claude\worktrees\confident-mestorf-6c00a5
pip install -e ".[dev]" -q
pytest tests/test_schemas.py -v -k "deploy_case"
```

Expected: `ImportError` or `ModuleNotFoundError` — `DeployCaseParams` does not exist yet.

- [ ] **Step 3: Add `DEPLOY_CASE` to `enums.py`**

In `src/task_worker_api/enums.py`, add after `CINEMATIC_BAKING`:

```python
    CINEMATIC_BAKING = "cinematic_baking"
    DEPLOY_CASE = "deploy_case"
```

- [ ] **Step 4: Create `src/task_worker_api/schemas/deploy_case.py`**

```python
"""Params schema for DEPLOY_CASE tasks.

Handled by the assetbundle-builder worker. `content_path` is the absolute
path to the exported case content folder on the shared volume, written by
the backend's export_case_model_collection() before the task is created.
`build_target` is passed as-is to the Unity CLI's -buildTarget flag.
"""
from __future__ import annotations

from typing import Optional

from ._base import TaskParamsBase


class DeployCaseParams(TaskParamsBase):
    """Input for the assetbundle-builder worker's deploy_case handler."""

    content_path: Optional[str] = None
    build_target: str = "Android"
```

- [ ] **Step 5: Register `DeployCaseParams` in `schemas/__init__.py`**

Add the import and registry entry:

```python
from .deploy_case import DeployCaseParams

TASK_PARAMS_SCHEMAS: dict[TaskType, type[TaskParamsBase]] = {
    TaskType.DETECT_CUT_PLANES: DetectCutPlanesParams,
    TaskType.MODEL_INITIALIZING: ModelInitializingParams,
    TaskType.CINEMATIC_BAKING: CinematicBakingParams,
    TaskType.GS_BUILD: GsBuildParams,
    TaskType.SEGMENTATION: SegmentationParams,
    TaskType.DEPLOY_CASE: DeployCaseParams,
}
```

Also add `"DeployCaseParams"` to `__all__`.

- [ ] **Step 6: Run all tests to verify they pass**

```bash
pytest -q
```

Expected: All tests pass. Schema tests for `deploy_case` show PASSED.

- [ ] **Step 7: Commit**

```bash
git add src/task_worker_api/enums.py \
        src/task_worker_api/schemas/deploy_case.py \
        src/task_worker_api/schemas/__init__.py \
        tests/test_schemas.py
git commit -m "feat: add DEPLOY_CASE task type and DeployCaseParams schema"
```

---

## Task 2: Regenerate TypeScript artifact + bump version

**Repo:** `task-worker-api`

**Files:**
- Regen: `artifacts/task-worker-types/index.ts`
- Modify: `pyproject.toml`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Regenerate the TypeScript artifact**

```bash
python tools/gen_typescript.py
```

Expected: `artifacts/task-worker-types/index.ts` is updated. Verify `DEPLOY_CASE = "deploy_case"` appears in the output.

- [ ] **Step 2: Bump version in `pyproject.toml`**

Change line 7:

```toml
version = "0.6.0"
```

- [ ] **Step 3: Add CHANGELOG entry**

Prepend to `CHANGELOG.md` after the `# Changelog` heading:

```markdown
## v0.6.0 — 2026-04-28

Adds `DEPLOY_CASE` task type for the Unity AssetBundle builder worker.

**New:**
- `TaskType.DEPLOY_CASE = "deploy_case"` in `enums.py`.
- `DeployCaseParams` schema: `content_path: Optional[str]` (absolute path to case content folder on shared volume), `build_target: str = "Android"`.
- Registered in `TASK_PARAMS_SCHEMAS`.
- TypeScript artifact updated.
```

- [ ] **Step 4: Run full test suite to verify nothing broken**

```bash
pytest -q
```

Expected: All tests pass.

- [ ] **Step 5: Commit + push to trigger CI release**

```bash
git add artifacts/task-worker-types/index.ts pyproject.toml CHANGELOG.md
git commit -m "chore: bump to v0.6.0 — add DEPLOY_CASE task type"
```

After merging to main, CI publishes the wheel at:
`https://github.com/SyngularXR/task-worker-api/releases/download/v0.6.0/task_worker_api-0.6.0-py3-none-any.whl`

---

## Task 3: SynPusher backend — concurrency config + pin update

**Repo:** `SynPusher-Vue`
**Working dir:** `p:\Project\SynPusher-Vue\services\backend`

**Files:**
- Modify: `src/database/task_models.py`
- Modify: `requirements.txt`

- [ ] **Step 1: Update `requirements.txt` pin**

Find the current `task-worker-api` line:
```
task-worker-api @ https://github.com/SyngularXR/task-worker-api/releases/download/v0.4.1/task_worker_api-0.4.1-py3-none-any.whl
```

Replace with:
```
task-worker-api @ https://github.com/SyngularXR/task-worker-api/releases/download/v0.6.0/task_worker_api-0.6.0-py3-none-any.whl
```

- [ ] **Step 2: Add `DEPLOY_CASE` to `MAX_CONCURRENT` in `task_models.py`**

Find the `MAX_CONCURRENT` dict (around line 105). Add:

```python
MAX_CONCURRENT: Dict[TaskType, int] = {
    TaskType.RENDER: 1,
    TaskType.GS_BUILD: _env_int("LOCAL_GS_WORKERS", 2),
    TaskType.SEGMENTATION: 1,
    TaskType.MODEL_INITIALIZING: 2,
    TaskType.APPLE_ML_GS: 1,
    TaskType.DETECT_CUT_PLANES: 2,
    TaskType.CINEMATIC_BAKING: _env_int("LOCAL_CINEMATIC_WORKERS", 1),
    TaskType.DEPLOY_CASE: 1,
}
```

- [ ] **Step 3: Add `DEPLOY_CASE` to `STALE_THRESHOLDS` in `task_models.py`**

Find the `STALE_THRESHOLDS` dict (around line 119). Add:

```python
STALE_THRESHOLDS: Dict[TaskType, timedelta] = {
    TaskType.RENDER: timedelta(minutes=10),
    TaskType.GS_BUILD: timedelta(minutes=30),
    TaskType.SEGMENTATION: timedelta(minutes=20),
    TaskType.MODEL_INITIALIZING: timedelta(minutes=5),
    TaskType.APPLE_ML_GS: timedelta(minutes=30),
    TaskType.DETECT_CUT_PLANES: timedelta(minutes=5),
    TaskType.CINEMATIC_BAKING: timedelta(minutes=10),
    TaskType.DEPLOY_CASE: timedelta(minutes=30),
}
```

- [ ] **Step 4: Reinstall backend deps and verify import works**

```bash
pip install -r requirements.txt -q
python -c "from task_worker_api.enums import TaskType; print(TaskType.DEPLOY_CASE)"
```

Expected: `TaskType.DEPLOY_CASE`

- [ ] **Step 5: Commit**

```bash
git add services/backend/src/database/task_models.py \
        services/backend/requirements.txt
git commit -m "feat(backend): add DEPLOY_CASE concurrency config, bump task-worker-api to v0.6.0"
```

---

## Task 4: SynPusher backend — extend deploy_case() to enqueue task

**Repo:** `SynPusher-Vue`

**Files:**
- Modify: `services/backend/src/routes/cases/model_collection.py`

- [ ] **Step 1: Read the current top of `model_collection.py`**

Confirm the existing imports so you know what's already in scope (`Task`, `TaskType`, `BuildStatus`, `Case`, `JSONResponse`, `export_case_model_collection`, `fetch_case_content_integration_status`).

- [ ] **Step 2: Add the missing imports if not already present**

At the top of `model_collection.py`, ensure these are imported:

```python
import logging
from pathlib import Path

from src.database.task_models import Task, TaskType
```

Add at module level:

```python
logger = logging.getLogger(__name__)
```

- [ ] **Step 3: Extend `deploy_case()` to create the task**

Replace the full `deploy_case` function body with:

```python
async def deploy_case(case_id: int):
    export_result = await export_case_model_collection(case_id)
    await Case.filter(id=case_id).update(
        content_integration_status=BuildStatus.IN_QUEUE.value
    )

    content_folder = str(Path(export_result["path"]).parent)
    try:
        await Task.create_or_reject(
            case_id=case_id,
            task_type=TaskType.DEPLOY_CASE,
            item_key="",
            params={"build_target": "Android", "content_path": content_folder},
        )
    except ValueError:
        logger.info(
            "DEPLOY_CASE task already active for case %s — skipping duplicate",
            case_id,
        )

    return JSONResponse(content={
        "success": True,
        "model_collection": export_result,
        "status": await fetch_case_content_integration_status(case_id),
    })
```

- [ ] **Step 4: Verify the backend starts without errors**

```bash
# From services/backend directory
python -c "from src.routes.cases.model_collection import deploy_case; print('OK')"
```

Expected: `OK`

- [ ] **Step 5: Smoke test via curl**

With the backend running locally:

```bash
# Replace <token> with a valid JWT from the SynPusher UI (browser devtools → Network → any request header)
# Replace 1 with a real case ID
curl -s -X POST http://localhost:5000/api/v1/deploy/1 \
  -H "Authorization: Bearer <token>" \
  | python -m json.tool
```

Expected response shape:
```json
{
  "success": true,
  "model_collection": { ... },
  "status": 1
}
```

Then verify the task row was created:
```bash
# In the backend Django/Tortoise shell or admin UI, check Task table for a DEPLOY_CASE row with case_id=1
```

- [ ] **Step 6: Commit**

```bash
git add services/backend/src/routes/cases/model_collection.py
git commit -m "feat(backend): enqueue DEPLOY_CASE task on deploy button click"
```

---

## Task 5: Worker — project scaffold

**Repo:** `syngar-ml-assetbundle-builder`
**Working dir:** `P:\Project\syngar-ml-assetbundle-builder`

**Files:**
- Create: `worker/pyproject.toml`
- Create: `worker/.env.example`
- Create: `worker/src/__init__.py`
- Create: `worker/src/handlers/__init__.py`

- [ ] **Step 1: Create `worker/pyproject.toml`**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "assetbundle-worker"
version = "0.1.0"
description = "Unity AssetBundle builder worker for the SynPusher task queue."
requires-python = ">=3.10"
license = { text = "Proprietary" }
authors = [{ name = "Syngular Technology" }]
dependencies = [
    "task-worker-api @ https://github.com/SyngularXR/task-worker-api/releases/download/v0.6.0/task_worker_api-0.6.0-py3-none-any.whl",
]

[project.optional-dependencies]
dev = [
    "pytest>=7.4",
    "pytest-asyncio>=0.23",
]

[tool.hatch.build.targets.wheel]
packages = ["src"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 2: Create `worker/.env.example`**

```dotenv
SYNPUSHER_URL=http://localhost:5000/api/v1
WORKER_API_KEY=your-api-key-here
WORKER_ID=assetbundle-builder-1
WORKER_WORKDIR=/tmp/assetbundle-worker
SHARED_VOLUME_PATH=/app/shared
ENABLE_TASK_WORKER=true
WORKER_POLL_INTERVAL=5
```

- [ ] **Step 3: Create empty init files**

Create `worker/src/__init__.py` — empty file.
Create `worker/src/handlers/__init__.py` — empty file.

- [ ] **Step 4: Install in editable mode and verify**

```bash
cd worker
pip install -e ".[dev]" -q
python -c "from task_worker_api.enums import TaskType; print(TaskType.DEPLOY_CASE)"
```

Expected: `TaskType.DEPLOY_CASE`

- [ ] **Step 5: Commit**

```bash
git add worker/pyproject.toml worker/.env.example worker/src/__init__.py worker/src/handlers/__init__.py
git commit -m "feat(worker): scaffold assetbundle-builder worker project"
```

---

## Task 6: Worker — entry point

**Repo:** `syngar-ml-assetbundle-builder`

**Files:**
- Create: `worker/src/sdk_worker.py`

- [ ] **Step 1: Create `worker/src/sdk_worker.py`**

```python
"""Entry point for the assetbundle-builder worker.

Uses the task-worker-api SDK. Handlers live in src.handlers and are
registered as native async def run(ctx, params) -> dict.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

from task_worker_api import TaskType, Worker

from src.handlers import deploy_case


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    if os.environ.get("ENABLE_TASK_WORKER", "false").lower() != "true":
        logging.info("ENABLE_TASK_WORKER is not 'true' — exiting")
        return 0

    backend_url = os.environ.get("SYNPUSHER_URL")
    api_key = os.environ.get("WORKER_API_KEY")
    if not backend_url or not api_key:
        logging.error("SYNPUSHER_URL and WORKER_API_KEY env vars are required")
        return 1

    worker = Worker(
        backend_url=backend_url.rstrip("/"),
        api_key=api_key,
        worker_id=os.environ.get("WORKER_ID", "assetbundle-builder-worker"),
        work_dir=os.environ.get("WORKER_WORKDIR", "/tmp/assetbundle-worker"),
        shared_volume_path=os.environ.get("SHARED_VOLUME_PATH") or None,
        poll_interval_s=float(os.environ.get("WORKER_POLL_INTERVAL", "5")),
        handlers={
            TaskType.DEPLOY_CASE: deploy_case.run,
        },
    )

    try:
        asyncio.run(worker.run_forever())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Verify the entry point imports cleanly**

```bash
cd worker
python -c "from src.sdk_worker import main; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add worker/src/sdk_worker.py
git commit -m "feat(worker): add sdk_worker entry point for assetbundle-builder"
```

---

## Task 7: Worker — deploy_case handler

**Repo:** `syngar-ml-assetbundle-builder`

**Files:**
- Create: `worker/src/handlers/deploy_case.py`

- [ ] **Step 1: Create `worker/src/handlers/deploy_case.py`**

```python
"""Handler for DEPLOY_CASE tasks.

Stub implementation: validates that the case content folder is non-empty.
The content_path param is the absolute path to the exported case content
folder on the shared volume, written by export_case_model_collection()
before the task is created.

Full Unity build integration (invoking build-bundle.sh) is a separate
session.
"""
from __future__ import annotations

import logging
from pathlib import Path

from task_worker_api import TaskContext
from task_worker_api.errors import TaskParamsError
from task_worker_api.schemas import DeployCaseParams

logger = logging.getLogger(__name__)


async def run(ctx: TaskContext, params: DeployCaseParams) -> dict:
    case_id = ctx.task.case_id

    if not params.content_path:
        raise TaskParamsError("content_path is required but was not provided in task params")

    case_folder = Path(params.content_path)

    if not case_folder.exists():
        raise TaskParamsError(f"case folder does not exist: {case_folder}")

    contents = list(case_folder.iterdir())
    if not contents:
        raise TaskParamsError(f"case folder is empty: {case_folder}")

    logger.info(
        "[STUB] case %s content folder OK (%d items) — build would run here (build_target=%s)",
        case_id,
        len(contents),
        params.build_target,
    )
    return {}
```

- [ ] **Step 2: Verify the handler imports cleanly**

```bash
cd worker
python -c "from src.handlers.deploy_case import run; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add worker/src/handlers/deploy_case.py
git commit -m "feat(worker): add deploy_case stub handler with empty-folder guard"
```

---

## Task 8: Worker — Dockerfile

**Repo:** `syngar-ml-assetbundle-builder`

**Files:**
- Create: `worker/Dockerfile`

- [ ] **Step 1: Create `worker/Dockerfile`**

```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir -e .

COPY src/ src/

ENV ENABLE_TASK_WORKER=true

CMD ["python", "-m", "src.sdk_worker"]
```

- [ ] **Step 2: Build the image to verify it builds**

```bash
cd worker
docker build -t assetbundle-worker:dev .
```

Expected: Build succeeds. Final layer runs `python -m src.sdk_worker`.

- [ ] **Step 3: Verify the image exits cleanly without env vars**

```bash
docker run --rm assetbundle-worker:dev
```

Expected: `SYNPUSHER_URL and WORKER_API_KEY env vars are required` and exit code 1.

- [ ] **Step 4: Commit**

```bash
git add worker/Dockerfile
git commit -m "feat(worker): add Python worker Dockerfile for assetbundle-builder"
```

---

## Task 9: Fleet registry update

**Repo:** `task-worker-api`

**Files:**
- Modify: `docs/fleet/workers.json`

- [ ] **Step 1: Update `sdk.current_release` to `v0.6.0`**

In `docs/fleet/workers.json`, change:
```json
"current_release": "v0.5.0",
"release_notes": "https://github.com/SyngularXR/task-worker-api/releases/tag/v0.5.0",
```
to:
```json
"current_release": "v0.6.0",
"release_notes": "https://github.com/SyngularXR/task-worker-api/releases/tag/v0.6.0",
```

- [ ] **Step 2: Append the assetbundle-builder worker entry**

Add to the `"workers"` array (after the `colmap-splat-worker` entry):

```json
{
  "id": "assetbundle-builder-worker",
  "repo": "https://github.com/SyngularXR/syngar-ml-assetbundle-builder",
  "image": "syngular/assetbundle-builder-worker",
  "image_tag_env": "ASSETBUNDLE_WORKER_TAG",
  "compose_service": "assetbundle-builder-worker",
  "mode": "polling",
  "mode_description": "Standalone Worker.run_forever() polling loop. Worker mode gated on ENABLE_TASK_WORKER=true. Unity build integration pending — handler currently validates content folder and stubs the build.",
  "task_types": ["deploy_case"],
  "sdk_pin": {
    "style": "wheel_url",
    "version": "v0.6.0",
    "file": "worker/pyproject.toml",
    "line_pattern": "task-worker-api @ https://github.com/SyngularXR/task-worker-api/releases/download/v{version}/task_worker_api-{version}-py3-none-any.whl"
  },
  "env": {
    "required": ["SYNPUSHER_URL", "WORKER_API_KEY", "WORKER_ID", "ENABLE_TASK_WORKER"],
    "optional": [
      "SHARED_VOLUME_PATH",
      "WORKER_WORKDIR",
      "WORKER_POLL_INTERVAL",
      "WORKER_PAYLOAD_LOG_ENABLED",
      "WORKER_PAYLOAD_LOG_RETENTION_DAYS",
      "WORKER_PAYLOAD_LOG_CLEANUP_INTERVAL_S"
    ]
  },
  "shared_volume_wired": true,
  "scaling": "single",
  "gpu": "none",
  "notes": "Worker lives under worker/ subdirectory; repo root Dockerfile is the Unity build container (separate concern, future integration)."
}
```

- [ ] **Step 3: Update `generated_at`**

Change `"generated_at": "2026-04-26"` to `"generated_at": "2026-04-28"`.

- [ ] **Step 4: Commit**

```bash
git add docs/fleet/workers.json
git commit -m "docs(fleet): add assetbundle-builder-worker, bump sdk current_release to v0.6.0"
```

---

## Task 10: End-to-end smoke test

**Goal:** Confirm `PENDING → CLAIMED → FAILED` with "empty folder" message.

**Prerequisites:**
- SynPusher backend running with updated `requirements.txt` (Task 3 committed)
- Worker running locally with `ENABLE_TASK_WORKER=true`, pointing at the backend
- An empty test directory to simulate a missing case content folder

- [ ] **Step 1: Start the worker locally**

```bash
cd P:\Project\syngar-ml-assetbundle-builder\worker
cp .env.example .env
# Edit .env: set SYNPUSHER_URL, WORKER_API_KEY, WORKER_ID
# Set SHARED_VOLUME_PATH to a local temp directory
ENABLE_TASK_WORKER=true python -m src.sdk_worker
```

Expected log: `polling for tasks…`

- [ ] **Step 2: Trigger a deploy via curl**

```bash
# Get a valid auth token from the SynPusher UI (browser → devtools → any request Authorization header)
curl -s -X POST http://localhost:5000/api/v1/deploy/1 \
  -H "Authorization: Bearer <your-jwt-token>" \
  | python -m json.tool
```

Expected: JSON response with `"success": true`.

- [ ] **Step 3: Watch the worker claim the task**

In the worker terminal, within 5 seconds you should see:

```
INFO task_worker_api.worker: claimed task <id> (deploy_case)
INFO src.handlers.deploy_case: [checking case folder...]
ERROR task_worker_api.worker: task <id> failed: case folder does not exist: /app/shared/content/...
```

The task status transitions: `PENDING(0) → CLAIMED(1) → FAILED(4)`.

- [ ] **Step 4: Verify the task failure in SynPusher**

Check the SynPusher task admin or query the DB:

```bash
curl -s http://localhost:5000/api/v1/tasks/<task-id> \
  -H "Authorization: Bearer <your-jwt-token>" \
  | python -m json.tool
```

Expected fields:
```json
{
  "status": 4,
  "error": "case folder does not exist: ..."
}
```

- [ ] **Step 5: Test with a non-empty folder**

Create a temp folder with a file in the path the task expects, or set `content_path` in the task params to a real folder, then re-trigger and confirm the worker logs `[STUB] build would run here` and the task reaches status `3` (COMPLETED).

---

## Self-review

**Spec coverage check:**
- ✅ `DEPLOY_CASE` enum + `DeployCaseParams` schema (Task 1)
- ✅ TypeScript artifact regen + v0.6.0 release (Task 2)
- ✅ Backend concurrency + stale config (Task 3)
- ✅ Backend endpoint extended with task creation (Task 4)
- ✅ Worker scaffold under `worker/` subdirectory (Tasks 5–8)
- ✅ Fleet registry updated (Task 9)
- ✅ End-to-end test with empty folder (Task 10)

**Refinement from spec:** `content_path` is passed in task params (set at creation from `export_result["path"]`) rather than fetched via a worker callback. This avoids auth complexity in the stub — the callback approach can be used in a future session when the worker has a dedicated worker-auth endpoint to call.

**Type consistency check:** `DeployCaseParams` uses `content_path: Optional[str]` in Task 1; handler reads `params.content_path` in Task 7 — consistent. `TaskParamsError` is imported from `task_worker_api.errors` in Task 7 — matches the SDK's public error module.
