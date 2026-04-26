# Runbook: Debugging a worker with captured payload logs

Every worker on `task-worker-api` v0.5.0+ writes one JSON line per claimed task to `/app/shared/_worker_payloads/{worker_id}/payloads-DATE-pidPID-BOOT.jsonl` inside its container. This runbook shows how to use those files to reproduce a failing task locally or feed real producer traffic into a new feature's tests.

## Prerequisites

- Worker is at `task-worker-api` v0.5.0+. (Check `workers.json` — `sdk_pin.version`.)
- Worker repo's entry point passes `shared_volume_path=os.environ.get("SHARED_VOLUME_PATH")` to `Worker(...)`. (Check `workers.json` — `shared_volume_wired: true`.)
- Worker container has `SHARED_VOLUME_PATH=/app/shared` set (it does in surgiclaw).

If any of those is wrong, payload logs aren't being written. See the [SDK upgrade runbook](sdk-upgrade.md) for the audit step.

## Find the right log file

```bash
# On the deploy host — log directory layout
docker exec blender-worker ls /app/shared/_worker_payloads/blender-worker-1/
# payloads-2026-04-26-pid12345-a1b2c3d4.jsonl
# raw_envelopes-2026-04-26-pid12345-a1b2c3d4.jsonl  (only present if protocol drift fired today)

# Find the line for a specific task_id
docker exec blender-worker bash -c \
  'grep "\"task_id\":12345" /app/shared/_worker_payloads/blender-worker-1/payloads-*.jsonl'
```

If the worker is scaled (multiple replicas with the same `WORKER_ID`), there's one `payloads-*.jsonl` file per replica per day — `cat *.jsonl | jq` aggregates them.

## Inspect a captured envelope

```bash
docker exec blender-worker bash -c \
  'grep "\"task_id\":12345" /app/shared/_worker_payloads/blender-worker-1/payloads-*.jsonl' | \
  jq '.'
```

A typed-stream entry looks like:

```json
{
  "captured_at": "2026-04-26T14:23:11.234567+00:00",
  "stream": "typed",
  "task_id": 12345,
  "task_type": "cinematic_baking",
  "case_id": 99,
  "item_key": "case_99_scene_01",
  "status": 2,
  "params": { "...": "..." },
  "worker_id": "blender-worker-1",
  "process_id": 12345,
  "boot_id": "a1b2c3d4"
}
```

The fields that matter for replay are `task_type`, `case_id`, `item_key`, and `params`. The rest is capture metadata.

## Replay against a backend

The transform — which fields to keep when re-enqueuing — is documented in the SDK's [adding-a-worker.md → Replaying captured payloads](../../adding-a-worker.md#replaying-captured-payloads) section. The short version:

| Field | Action |
|---|---|
| `task_type`, `case_id`, `item_key`, `params` | **Keep** — these are the task spec |
| `task_id`, `status`, `worker_id`, `captured_at`, `stream`, `process_id`, `boot_id` | **Drop** — capture metadata, not task spec |

A 10-line replay script is included in the linked section.

## Reproduce locally without re-enqueuing

If you want to run the captured task through the worker handler in a debugger without going through the backend:

```python
import json
from pathlib import Path

from task_worker_api import TaskType
from task_worker_api.context import ClaimedTask, FileContext, TaskContext
from task_worker_api.enums import TaskStatus
from task_worker_api.schemas import TASK_PARAMS_SCHEMAS

# Pick a captured line
line = Path("payloads-2026-04-26-pid12345-a1b2c3d4.jsonl").read_text().splitlines()[0]
e = json.loads(line)

# Recreate the typed task and call the handler directly
task = ClaimedTask(
    id=e["task_id"],
    task_type=TaskType(e["task_type"]),
    case_id=e["case_id"],
    item_key=e["item_key"],
    status=TaskStatus(e["status"]),
    params=e["params"],
    worker_id=e["worker_id"],
)
typed_params = TASK_PARAMS_SCHEMAS[task.task_type](**task.params)
# Now call the handler: handler(ctx, typed_params)
```

You'll need a real `TaskContext` (with `FileContext` pointing at staged inputs) for handlers that touch the filesystem; see `tests/test_worker_loop.py` in any worker repo for setup patterns.

## Investigating protocol drift (raw_envelopes stream)

If `raw_envelopes-*.jsonl` exists with content, the backend returned an envelope `BackendClient.claim_next` couldn't parse:

```bash
docker exec colmap-splat-worker-1 cat /app/shared/_worker_payloads/colmap-splat-worker-1/raw_envelopes-*.jsonl | jq '.'
```

A raw entry looks like:

```json
{
  "captured_at": "2026-04-26T14:23:11Z",
  "stream": "raw",
  "raw": { "id": 99, "task_type": "unknown_future_type", "...": "..." },
  "error_type": "ValueError",
  "error": "'unknown_future_type' is not a valid TaskType",
  "worker_id": "colmap-splat-worker-1",
  "process_id": 12345,
  "boot_id": "a1b2c3d4"
}
```

This almost always means the backend deployed a new `task_type` before the worker fleet was upgraded. Action:

1. Check the SDK's `enums.py` to see if the new task_type was added in a release the worker hasn't picked up yet.
2. If yes, follow the [SDK upgrade runbook](sdk-upgrade.md) for that worker (a worker that doesn't handle the new type wouldn't be filtering for it anyway, but it's evidence the backend is ahead of at least some worker).
3. If no, the backend has shipped a type that doesn't exist in the SDK. File an issue on `task-worker-api` to add the schema.

## Disabling capture

Per-deployment, set `WORKER_PAYLOAD_LOG_ENABLED=false` in `surgiclaw/.env`. Cleanup of existing files still runs, so disabling doesn't leave you with disk pressure.

To keep capture but prune harder, lower `WORKER_PAYLOAD_LOG_RETENTION_DAYS` from the default 14.

## Disk pressure

Worst case under realistic loads: ~hundreds of tasks/day × ~1KB params each × 14 days × 3 workers ≈ low-MB total. Per-record cap is 256KB, so pathological payloads top out at hundreds-of-MB even under sustained truncation. If you see runaway growth, check whether something is repeatedly enqueueing identical large tasks — the cap protects the worker disk, but doesn't stop the producer from filling its own disk if it's logging the same payloads.
