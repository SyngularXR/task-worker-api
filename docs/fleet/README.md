# Worker Fleet HQ

This is the central reference for every `task-worker-api` consumer in the SyngularXR fleet. If you're adding a new worker, upgrading the SDK across the fleet, debugging an existing worker, or wiring a backend to the worker pool, **start here**.

## What lives in this directory

| File | What it is | Audience |
|---|---|---|
| [`workers.json`](workers.json) | Machine-readable manifest of every worker — repo, image, task types, env contract, current SDK pin. **Source of truth** for backends/automation. | Backend code (SynPusher-Vue's Nexus Core), CI tooling, fleet automation |
| [`README.md`](README.md) (this file) | Human-readable index and quick reference | Operators, new contributors |
| [`conventions.md`](conventions.md) | Fleet-wide conventions: dep pinning style, env var contract, `shared_volume_path` wiring, payload logging | Worker repo authors |
| [`runbooks/sdk-upgrade.md`](runbooks/sdk-upgrade.md) | Step-by-step playbook for bumping `task-worker-api` across the fleet | Anyone shipping an SDK release |
| [`runbooks/local-testing.md`](runbooks/local-testing.md) | Pull latest worker images and restart the local compose stack | Dev box / staging operators |
| [`runbooks/debugging-with-payload-logs.md`](runbooks/debugging-with-payload-logs.md) | Replay captured task envelopes for reproducing bugs | Worker debuggers |

The companion [`docs/adding-a-worker.md`](../adding-a-worker.md) is the deeper "build a new worker from scratch" guide; this directory focuses on **fleet-wide concerns** rather than per-worker SDK usage.

## The fleet at a glance

| Worker | Repo | Image | Task types | Mode | Scaling |
|---|---|---|---|---|---|
| `neural-canvas` | [Neural-Canvas](https://github.com/SyngularXR/Neural-Canvas) | `syngular/neural-canvas` | `segmentation` | hybrid (FastAPI + worker) | single |
| `blender-worker` | [Blender-CLI](https://github.com/SyngularXR/Blender-CLI) | `syngular/blender-worker` | `optimize`, `uv_unwrap`, `render`, `pipeline`, `cinematic_baking`, `model_initializing`, `detect_cut_planes` | polling | single |
| `colmap-splat-worker` | [colmap-splat](https://github.com/SyngularXR/colmap-splat) | `syngular/colmap-splat-worker` | `gs_build` | polling | horizontal |

For the canonical, machine-readable version of this table — **including current SDK pin per worker, env var contracts, and links** — see [`workers.json`](workers.json).

The deployment lives in [`syngar-deployment-scripts/surgiclaw`](https://github.com/SyngularXR/syngar-deployment-scripts/tree/main/surgiclaw): every worker mounts `${SHARED_DATA_PATH}:/app/shared` cross-platform on Linux Docker and Windows Docker Desktop.

## Reading the manifest from automation

`workers.json` is intended to be fetched at build time or runtime by anything that needs to reason about the fleet:

```bash
# Raw URL — always tracks main
curl -s https://raw.githubusercontent.com/SyngularXR/task-worker-api/main/docs/fleet/workers.json | jq '.workers[].id'
# → "neural-canvas"
# → "blender-worker"
# → "colmap-splat-worker"

# Which worker handles a given task type?
curl -s https://raw.githubusercontent.com/SyngularXR/task-worker-api/main/docs/fleet/workers.json | \
  jq -r '.workers[] | select(.task_types | index("gs_build")) | .id'
# → colmap-splat-worker
```

A SynPusher-Vue backend that wants to render a "fleet status" page or drive deployment health checks can poll the raw URL and key off `image_tag_env` + `compose_service` to walk the deployment.

## Common workflows — quick links

- **Bumping `task-worker-api` SDK across all workers** → [`runbooks/sdk-upgrade.md`](runbooks/sdk-upgrade.md)
- **Pulling latest images on the deploy host** → [`runbooks/local-testing.md`](runbooks/local-testing.md)
- **Reproducing a worker bug from a captured task envelope** → [`runbooks/debugging-with-payload-logs.md`](runbooks/debugging-with-payload-logs.md)
- **Adding a new worker to the fleet** → [`../adding-a-worker.md`](../adding-a-worker.md), then update [`workers.json`](workers.json) with its entry.

## When to update what

| If you change... | You must update... |
|---|---|
| A worker's task_types | `workers.json` (the worker's `task_types` array) |
| A worker's image name or tag env | `workers.json` and `surgiclaw/docker-compose.yml` |
| The SDK release version | Each worker's `sdk_pin.version` in `workers.json` (one per row) |
| The required env var contract | `workers.json` `common_env_vars` (if fleet-wide) or per-worker `env.required` |
| A new shared convention (e.g., new env var, new directory layout) | [`conventions.md`](conventions.md) |
| The SDK upgrade procedure | [`runbooks/sdk-upgrade.md`](runbooks/sdk-upgrade.md) |

## Stale-detection

If `workers.json`'s `sdk.current_release` doesn't match a worker's `sdk_pin.version`, that worker is behind. The SDK upgrade runbook automates the catch-up.

If a worker repo's `Worker(...)` constructor doesn't pass `shared_volume_path`, payload logging silently disables. The audit step in the SDK-upgrade runbook is what catches this; record the result in `workers.json`'s `shared_volume_wired` field for future automation.
