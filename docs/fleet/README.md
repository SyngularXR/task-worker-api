# Worker Fleet HQ

This is the central reference for every `task-worker-api` consumer in the SyngularXR fleet. If you're adding a new worker, upgrading the SDK across the fleet, debugging an existing worker, or wiring a backend to the worker pool, **start here**.

## What lives in this directory

| File | What it is | Audience |
|---|---|---|
| [`workers.json`](workers.json) | Machine-readable manifest of every worker — repo, image, task types, env contract, current SDK pin. **Source of truth** for backends/automation. | Backend code (SynPusher-Vue's Nexus Core), CI tooling, fleet automation |
| [`README.md`](README.md) (this file) | Human-readable index and quick reference | Operators, new contributors |
| [`conventions.md`](conventions.md) | Fleet-wide conventions: dep pinning style, env var contract, `shared_volume_path` wiring, payload logging | Worker repo authors |
| [`workflow-standard.md`](workflow-standard.md) | Common workflow diagram format, SDK lifecycle reference and improvement evidence rules | Agents, worker authors and reviewers |
| [`improvement-scopes.md`](improvement-scopes.md) | Per-worker improvement scopes, visual/Android performance gates, defect checks and upstream review contract | Agents, worker authors and reviewers |
| [`runbooks/sdk-upgrade.md`](runbooks/sdk-upgrade.md) | Step-by-step playbook for bumping `task-worker-api` across the fleet | Anyone shipping an SDK release |
| [`runbooks/local-testing.md`](runbooks/local-testing.md) | Pull latest worker images and restart the local compose stack | Dev box / staging operators |
| [`runbooks/debugging-with-payload-logs.md`](runbooks/debugging-with-payload-logs.md) | Replay captured task envelopes for reproducing bugs | Worker debuggers |

The companion [`docs/adding-a-worker.md`](../adding-a-worker.md) is the deeper "build a new worker from scratch" guide; this directory focuses on **fleet-wide concerns** rather than per-worker SDK usage.

## Worker workflow guides

All worker diagrams follow the [workflow documentation standard](workflow-standard.md).
Agents should read it first, then the current guide and its linked implementation.
Apply the [improvement scopes](improvement-scopes.md) when proposing quality,
generation-speed, real-time performance, dependency or bug-fix changes.
The guides distinguish source behavior from deployed images, and identify SDK,
handler/child and backend publication ownership consistently.

| Worker | Maintained workflow guide | Implemented scope |
|---|---|---|
| Blender | [Model build pipeline](https://github.com/SyngularXR/Blender-CLI/blob/main/docs/model-build-pipeline.md) | Initialization, cinematic baking and cut-plane detection; geometry/UV/bake budgets |
| Neural-Canvas | [Worker task pipelines](https://github.com/SyngularXR/Neural-Canvas/blob/main/docs/guide/worker-pipelines.md) | Segmentation, spatial reconstruction and conditional synthetic dispatch |
| colmap-splat | [Gaussian build pipelines](https://github.com/SyngularXR/colmap-splat/blob/main/docs/worker-pipelines.md) | Scene/spatial builds, COLMAP/cache paths, training, export and cropping |
| AssetBundle builder | [Bundle pipeline](https://github.com/SyngularXR/syngar-ml-assetbundle-builder/blob/main/docs/worker-pipeline.md) | Snapshot/Unity build, licensing, validation, publication and measured timings |
| Backend compute | [Admitted compute workflows](https://github.com/SyngularXR/SynPusher-Vue/blob/main/docs/guide/backend-compute-worker.md) | Render, GS4D phase preparation/sequence assembly and deploy snapshot preparation |
| Backend finalizer | [Trusted publication workflows](https://github.com/SyngularXR/SynPusher-Vue/blob/main/docs/guide/backend-finalizer-worker.md) | Ten CPU finalizer routes, publication ownership, validation and recovery boundaries |
| Synthetic reconstruction | [Synthetic pipeline](https://github.com/SyngularXR/synthetic-generator/blob/main/docs/worker-pipeline.md) | Input/ROI preparation, NiftyMIC reconstruction, volume output, admitted cleanup and DICOM publication handoff |
| Visual tracking foundation | [Benchmark workflow](https://github.com/SyngularXR/visual-tracking-worker/blob/main/docs/worker-pipeline.md) | CPU protocol-control replay, independent evaluation and admitted adapter; not enrolled/deployed, no real tracker |

For an improvement, identify the affected stage and output contract, compare the
same original input with recorded settings/source/image/hardware, and attach
timing plus final-quality evidence to the PR. Update the worker guide with the
change. Handler availability, heartbeat, child completion, artifact commit,
backend publication and resource release are distinct facts.

These guides cover all seven operational roles (`s4-blender`, `s4-gs`,
`s4-neural`, `s4-assetbundle`, `s4-backend-compute`, `s4-backend-finalizer`,
`s4-synthetic`) plus the tracking foundation. They are a source index, not an
enrollment record. Apply each role's [improvement scope](improvement-scopes.md#worker-scopes)
and verify live deployment separately; the manifest below is a narrower inventory.

## The fleet at a glance

| Worker | Repo | Image | Task types | Mode | Scaling |
|---|---|---|---|---|---|
| `neural-canvas` | [Neural-Canvas](https://github.com/SyngularXR/Neural-Canvas) | `syngular/neural-canvas` | `segmentation`, conditional `spatial_recon` and `generate_synthetic` (see guide) | hybrid (FastAPI + worker) | single |
| `blender-worker` | [Blender-CLI](https://github.com/SyngularXR/Blender-CLI) | `syngular/blender-worker` | `cinematic_baking`, `model_initializing`, `detect_cut_planes` | polling | single |
| `colmap-splat-worker` | [colmap-splat](https://github.com/SyngularXR/colmap-splat) | `syngular/colmap-splat-worker` | `gs_build`, `spatial_gs_build` | polling | horizontal |
| `assetbundle-builder-worker` | [syngar-ml-assetbundle-builder](https://github.com/SyngularXR/syngar-ml-assetbundle-builder) | `syngular/assetbundle-builder-worker` | `deploy_case` | polling | single |

For machine-readable fleet configuration, SDK pins and environment contracts,
see [`workers.json`](workers.json). Inventory can lag handler changes; use the
maintained guides and linked registration code to verify current support, and
check the deployed image separately for a live host.

Deploy Surgiclaw services through [`surgiclaw-deploy`](https://github.com/SyngularXR/surgiclaw-deploy).
Its [compose bundle](https://github.com/SyngularXR/surgiclaw-deploy/blob/main/bundle/compose/docker-compose.yml)
defines the worker mounts and service configuration.

## Reading the manifest from automation

`workers.json` is intended to be fetched at build time or runtime by anything that needs to reason about the fleet:

```bash
# Raw URL — always tracks main
curl -s https://raw.githubusercontent.com/SyngularXR/task-worker-api/main/docs/fleet/workers.json | jq '.workers[].id'
# → "neural-canvas"
# → "blender-worker"
# → "assetbundle-builder-worker"
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
| A workflow stage, gate, budget, output or lifecycle boundary | The worker's maintained guide using [`workflow-standard.md`](workflow-standard.md); keep its README/AGENTS links current |
| The SDK upgrade procedure | [`runbooks/sdk-upgrade.md`](runbooks/sdk-upgrade.md) |

## Stale-detection

If `workers.json`'s `sdk.current_release` doesn't match a worker's `sdk_pin.version`, that worker is behind. The SDK upgrade runbook automates the catch-up.

If a worker repo's `Worker(...)` constructor doesn't pass `shared_volume_path`, payload logging silently disables. The audit step in the SDK-upgrade runbook is what catches this; record the result in `workers.json`'s `shared_volume_wired` field for future automation.
