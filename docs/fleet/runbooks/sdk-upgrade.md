# Runbook: Bumping `task-worker-api` across the fleet

Codifies the multi-repo upgrade dance done for v0.4.1 → v0.5.0. Follow this when shipping any SDK release that worker repos should pick up.

## Pre-flight

1. **Read the SDK CHANGELOG entry** for the new release. Note any breaking changes — especially anything that requires worker repos to change code (not just bump pins).
2. **Open [`docs/fleet/workers.json`](../workers.json)**. The `sdk_pin` block per worker tells you which file to edit and which pinning style each repo uses. Update `sdk.current_release` at the top once the SDK PR merges.
3. **Confirm wheel publication.** The SDK's release workflow auto-publishes the wheel asset to GitHub Releases on every main merge. Worker PRs that pin via wheel URL will fail their docker builds with a 404 if you bump the pin before the wheel is published.

## Order of operations

1. **SDK PR** lands first. The release workflow runs on merge to main — wait for it to finish (typically <30s for the publish step). Verify:
   ```bash
   gh release view vX.Y.Z --repo SyngularXR/task-worker-api --json tagName,assets
   ```
2. **Deployment PR** (optional, parallel-safe). If this SDK release adds new fleet-wide env vars, update `surgiclaw/.env.example` + `surgiclaw/docker-compose.yml`. Can land before worker PRs because env vars are inert until workers ship.
3. **Worker PRs** — one per consuming repo. Each is a small dep-pin edit; see per-repo recipes below.
4. **Container rebuild** is automatic via each worker repo's CI on merge. After all worker PRs merge, follow the [local-testing runbook](local-testing.md) to pull on the deploy host.

## Per-repo recipes

For each entry in `workers.json`'s `workers[]` array, follow the recipe matching that worker's `sdk_pin.style`.

### Style: `git_ref` (Neural-Canvas, colmap-splat)

```bash
cd <worker-repo>
git checkout main && git pull
git checkout -b feat/task-worker-api-vX.Y.Z

# Edit requirements.txt — change the pinned version
# task-worker-api @ git+https://github.com/SyngularXR/task-worker-api.git@vX.Y.Z

git add requirements.txt
git commit -m "deps: bump task-worker-api to vX.Y.Z"
git push -u origin feat/task-worker-api-vX.Y.Z
gh pr create --title "deps: bump task-worker-api to vX.Y.Z" --body "..."
```

**colmap-splat caveat:** The Dockerfile must pre-install `hatchling` because requirements.txt installs run with `--no-build-isolation` for torch/CUDA pinning. Without it, `pip install task-worker-api @ git+https://...` fails at the metadata-prep step. This was added in the v0.5.0 rollout.

### Style: `wheel_url` (Blender-CLI)

```bash
cd <worker-repo>
git checkout main && git pull
git checkout -b feat/task-worker-api-vX.Y.Z

# Edit pyproject.toml — change the wheel URL
# "task-worker-api @ https://github.com/SyngularXR/task-worker-api/releases/download/vX.Y.Z/task_worker_api-X.Y.Z-py3-none-any.whl",

git add pyproject.toml
git commit -m "deps: bump task-worker-api wheel to vX.Y.Z"
git push -u origin feat/task-worker-api-vX.Y.Z
gh pr create --title "deps: bump task-worker-api wheel to vX.Y.Z" --body "..."
```

The wheel URL must exist before this PR's CI runs, otherwise pytest install fails with a 404.

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
