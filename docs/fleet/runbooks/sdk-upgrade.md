# Runbook: Bumping `task-worker-api` across the fleet

Codifies the multi-repo upgrade dance done for v0.4.1 → v0.5.0. Follow this when shipping any SDK release that worker repos should pick up.

## Pre-flight

1. **Read the SDK CHANGELOG entry** for the new release. Note any breaking changes — especially anything that requires worker repos to change code (not just bump pins).
2. **Open [`docs/fleet/workers.json`](../workers.json)**. The `sdk_pin` block per worker records each repo's pinning style and primary pin file — most repos carry the version in more files than that one, and the full list is in the [per-repo recipes](#per-repo-recipes) below. Update `sdk.current_release` at the top once the SDK PR merges.
3. **Confirm wheel publication.** The SDK's release workflow auto-publishes the wheel asset to GitHub Releases on every main merge. Worker PRs that pin via wheel URL will fail their docker builds with a 404 if you bump the pin before the wheel is published.

## Order of operations

1. **SDK PR** lands first. The release workflow runs on merge to main — wait for it to finish (typically <30s for the publish step). Verify:
   ```bash
   gh release view vX.Y.Z --repo SyngularXR/task-worker-api --json tagName,assets
   ```
2. **Deployment PR** (optional, parallel-safe). If this SDK release adds new fleet-wide env vars, update `surgiclaw/.env.example` + `surgiclaw/docker-compose.yml`. Can land before worker PRs because env vars are inert until workers ship.
3. **Worker PRs** — one per consuming repo. Each is a small dep-pin edit; see per-repo recipes below.
4. **Container rebuild** is automatic via each worker repo's CI on merge. After all worker PRs merge, follow the [local-testing runbook](local-testing.md) to pull on the deploy host. The exception is admitted (protocol v2) workers: their images are hand-built, so they stay on the old SDK until someone rebuilds and releases them — see [Admission images](#admission-images).

## Per-repo recipes

Each worker repo carries the pin in up to three places, and they must move together in one PR — each repo's own historical bump commits do. Verified against each repo's `origin/main` on 2026-09-18:

| Repo | Pin file (style) | Also carries the version | Typical PR CI |
|---|---|---|---|
| Neural-Canvas | `requirements.txt` (`wheel_url`) | `.github/workflows/unit_tests.yml`, `docker/Dockerfile.admission` | ~2 min |
| colmap-splat | `requirements.txt` (`git_ref`) | `.github/workflows/build.yml`, `Dockerfile.admission` | up to ~17 min (docker build) |
| syngar-ml-assetbundle-builder | `worker/pyproject.toml` (`git_ref`) | `Dockerfile.admission` | ~3 min |
| Blender-CLI | `pyproject.toml` (`wheel_url`) | — | 11–19 min (docker build) |

No lockfile or doc carries the current version, so the practical recipe is "replace the old version string in every one of these files". (assetbundle-builder's `worker/uv.lock` still says `v0.13.0`, but nothing installs from it — CI and the Dockerfile both `pip install` the `worker/` package — so leave it alone.) A quick check, in each repo:

```bash
git fetch origin
git grep -n "0\.19\.0\.dev<old>" origin/main   # before: every file you need to edit
git grep -n "0\.19\.0\.dev<old>"               # after editing: none of them should still match
```

`<old>` is per repo — don't assume the fleet is level (in the dev47 rollout Blender-CLI was on dev45 while the other three were on dev35). A hit outside the table is either history (a CHANGELOG entry, an old plan doc — leave it) or a new pin location — add it to the table.

Whatever the repo's style, its `Dockerfile.admission` names the wheel *file* (`task_worker_api-X.Y.Z-py3-none-any.whl`, three times). Editing that file is not the same as shipping it — see [Admission images](#admission-images).

### Style: `git_ref` (colmap-splat, syngar-ml-assetbundle-builder)

```bash
cd <worker-repo>
git checkout main && git pull
git checkout -b feat/task-worker-api-vX.Y.Z

# Edit every file in this repo's row above — change the pinned version
# task-worker-api @ git+https://github.com/SyngularXR/task-worker-api.git@vX.Y.Z

git add <the files you edited>
git commit -m "deps: bump task-worker-api to vX.Y.Z"
git push -u origin feat/task-worker-api-vX.Y.Z
gh pr create --title "deps: bump task-worker-api to vX.Y.Z" --body "..."
```

**colmap-splat caveat:** The Dockerfile must pre-install `hatchling` because requirements.txt installs run with `--no-build-isolation` for torch/CUDA pinning. Without it, `pip install task-worker-api @ git+https://...` fails at the metadata-prep step. This was added in the v0.5.0 rollout.

### Style: `wheel_url` (Neural-Canvas, Blender-CLI)

```bash
cd <worker-repo>
git checkout main && git pull
git checkout -b feat/task-worker-api-vX.Y.Z

# Edit every file in this repo's row above — change the wheel URL
# "task-worker-api @ https://github.com/SyngularXR/task-worker-api/releases/download/vX.Y.Z/task_worker_api-X.Y.Z-py3-none-any.whl",

git add <the files you edited>
git commit -m "deps: bump task-worker-api wheel to vX.Y.Z"
git push -u origin feat/task-worker-api-vX.Y.Z
gh pr create --title "deps: bump task-worker-api wheel to vX.Y.Z" --body "..."
```

The wheel URL must exist before this PR's CI runs, otherwise pytest install fails with a 404.

## Admission images

Merging a pin-bump PR does **not** change admitted (protocol v2) workers. Each `Dockerfile.admission` does

```dockerfile
COPY task_worker_api-X.Y.Z-py3-none-any.whl /tmp/
```

from a hand-prepared build context, on top of a locally built `codex/*:admission-devNN` base image (its `ARG BASE_IMAGE` line). It is not built by CI, and the wheel is not in the repo. The merged PR only records what the *next* admission image should contain; to get there, someone must:

1. **Stage the new wheel** in the build context — download it from the GitHub release:
   ```bash
   gh release download vX.Y.Z --repo SyngularXR/task-worker-api --pattern "*.whl"
   ```
2. **Rebuild the admission images** from each repo's `Dockerfile.admission`.
3. **Ship a Windows admission release** per [`syngar-deployment-scripts/surgiclaw/admission/README.md`](https://github.com/SyngularXR/syngar-deployment-scripts/blob/main/surgiclaw/admission/README.md): the new image references go in `images.json`, packaged with `surgiclaw/scripts/new-windows-release.ps1`.

The host supervisor runtime vendors the SDK separately, under `vendor/` in that release's runtime directory — rebuilding worker images doesn't touch it.

Blender-CLI has no `Dockerfile.admission` (its one `Dockerfile` does `pip install .`, so the CI-built image follows `pyproject.toml`), but `images.json` pins every worker image by digest, so an admission box still only gets it through step 3.

Say in each pin-bump PR that admitted workers stay on the old SDK until this is done.

## Staged rollout for opt-in behaviour changes

Some SDK releases add a knob that is inert until a consumer sets it — the release is a no-op by design, and the behaviour change lands per worker, on your schedule. `retry_sleep_budget_s` (the retry-sleep budget, default `None` = unbounded, recommended value `600.0`) is the current example. It bounds the sleeps a call may *start*, not wall clock — a sleep already in flight is never interrupted, so a handler that blocks the event loop can overrun the budget by however long it blocked. Pick a value with headroom for that; don't read it as a hard ceiling. Roll these out in three passes, never in one:

1. **Release the no-op SDK.** Land the SDK PR and let the wheel publish. Nothing in the fleet changes behaviour — every consumer still gets the old semantics because the new knob defaults to off.
2. **Bump each consumer's pin** using the per-repo recipes above. Still a no-op: the worker now *has* the knob, but isn't passing it.
3. **Enable it, service by service**, only after that consumer's pin is merged and its image rebuilt on the compatible SDK. Update that worker's `Worker(...)` construction (or the config/env it reads) to pass the value — for the retry budget:
   ```python
   Worker(
       ...,
       retry_sleep_budget_s=600.0,   # give up after 10 min of retry backoff
   )
   ```
   Then flip the corresponding deployment setting for that one service in `syngar-deployment-scripts` and watch it for a full task cycle before moving to the next. One worker at a time keeps a bad value from taking the whole fleet down, and keeps the blame obvious if throughput changes.

Never enable a setting on a consumer that hasn't pinned the SDK release that understands it — passing an unknown keyword to `Worker(...)`/`BackendClient(...)` is a `TypeError` at construction, which crashes the worker at startup rather than degrading.

## Audit step (do every time)

For each worker, verify the entry point passes `shared_volume_path`:

```bash
grep -n "shared_volume_path" <worker-repo>/src/.../sdk_worker.py
# Expect: shared_volume_path=os.environ.get("SHARED_VOLUME_PATH")
```

If missing, the SDK's volume-mounted features (file staging, payload logging) silently disable. **Add the wiring before bumping the SDK** — otherwise the new release's value isn't realised.

The audit result is recorded in `workers.json` under `shared_volume_wired`. Update it if anything changes.

## After all worker PRs merge

1. Update [`docs/fleet/workers.json`](../workers.json):
   - Bump `sdk.current_release`.
   - Bump each worker's `sdk_pin.version` to match the merged PR.
2. Open the local-testing runbook to roll out: [`runbooks/local-testing.md`](local-testing.md).

## Rollback

If a worker PR's CI passes but the rebuilt image is broken in production:

1. Revert the dep-bump commit on that repo's main: `git revert <commit-sha>`. CI rebuilds with the previous SDK pin.
2. **Don't** revert the SDK release — pin reversion at the consumer is faster and doesn't disrupt other workers.
3. Open an issue on `task-worker-api` with the failure mode, reproduce locally, fix forward.

## v0.4.1 → v0.5.0 example (post-mortem)

The v0.5.0 rollout took 5 PRs across 3 repos plus the deployment repo:

| PR | Repo | Outcome |
|---|---|---|
| #4 | task-worker-api (SDK) | merged, wheel published 03:54:58Z |
| #49 | syngar-deployment-scripts | merged, env vars added to `.env.example` + each worker's `environment:` block |
| #100 | Neural-Canvas | merged, dep bumped, plus pre-existing HMAC-router work bundled |
| #17 | colmap-splat | merged, dep bumped + Dockerfile hatchling pre-install |
| #29 | Blender-CLI | held — main is red on unrelated pre-existing tests; pin bump pending until the test failures are fixed |

What we'd do faster next time:
- Pre-check each worker repo's CI baseline before opening dep-bump PRs (so red mains aren't a surprise).
- Run the audit step before opening any PRs (so you know if a worker silently won't take the new feature).

## v0.19.0.dev35 → v0.19.0.dev47 example (2026-09-18)

A pins-only rollout — no worker code changed, every edit was a version-string replacement: 1 SDK PR plus 4 worker PRs touching 9 files.

| PR | Repo | Outcome |
|---|---|---|
| [#104](https://github.com/SyngularXR/task-worker-api/pull/104) | task-worker-api (SDK) | merged 11:39:08Z, wheel published 11:39:24Z — v2 fix: attempt-lease renewal bounded by the lease's remaining time |
| [#243](https://github.com/SyngularXR/Neural-Canvas/pull/243) | Neural-Canvas | open, CI green (~1 min) — 3 files, dev35 → dev47 |
| [#73](https://github.com/SyngularXR/colmap-splat/pull/73) | colmap-splat | open, CI green (8 min) — 3 files, dev35 → dev47 |
| [#88](https://github.com/SyngularXR/syngar-ml-assetbundle-builder/pull/88) | syngar-ml-assetbundle-builder | open, CI green (~3.5 min) — 2 files, dev35 → dev47 |
| [#120](https://github.com/SyngularXR/Blender-CLI/pull/120) | Blender-CLI | open, CI green (~15 min) — 1 file, dev45 → dev47 |

Worker PR states as of 2026-09-18 12:12Z: all four mergeable, none merged yet.

What this one taught:
- `workers.json` and this runbook each named one pin file per repo; the repos had up to three, and assetbundle-builder was missing here altogether. Grep `origin/main` (the quick check above) rather than trusting either.
- The fleet wasn't level — Blender-CLI was already on dev45 — so "the old version" is a per-repo question.
- #104 only touches protocol v2 code, which only admitted workers run — exactly the workers a pin merge doesn't reach. The fix lands when the [admission images](#admission-images) are rebuilt and released, not when these PRs merge.
