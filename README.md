# task-worker-api

Shared contract and worker SDK for the [SynPusher](https://github.com/SyngularXR/SynPusher-Vue) task queue.

## Status

**v0.1.0 — unstable.** Minimum-viable scaffold carrying the schemas needed to unblock `detect_cut_planes`. Full worker SDK (HTTP client, cancel patterns, file transfer, hybrid mode) lands in follow-up phases. See [the design spec](https://github.com/SyngularXR/SynPusher-Vue/blob/main/docs/specs/2026-04-22-unified-task-queue-api-contract-design.md).

## Install

Local editable:

```
pip install -e P:\Project\task-worker-api
```

Once the repo is published to GitHub:

```
pip install task-worker-api @ git+https://github.com/SyngularXR/task-worker-api.git@v0.1.0
```

## What's in v0.1.0

- `task_worker_api.enums` — `TaskType`, `TaskStatus`
- `task_worker_api.schemas` — Pydantic params schemas per TaskType, `TASK_PARAMS_SCHEMAS` registry
- `task_worker_api.errors` — `TaskCancelled`, `TaskParamsError`, `ProtocolError`

## What's NOT in v0.1.0

- HTTP client, file transfer, cancel guard, progress reporter, Worker class, hybrid mode runner, testing fixtures. These land as the spec's Phase 1 completes.
