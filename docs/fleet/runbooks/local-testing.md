# Runbook: Local-container testing with latest images

After worker PRs merge, each repo's CI rebuilds + pushes its container image. This runbook covers pulling those images on the deploy host and restarting the affected services.

## On a deploy host running surgiclaw

```bash
cd /path/to/syngar-deployment-scripts/surgiclaw

# Pull all worker images (skips services with locally-built images)
docker compose pull neural-canvas blender-worker colmap-splat-worker

# Recreate the workers without disturbing nexus-core/db/frontend
docker compose up -d --no-deps neural-canvas blender-worker colmap-splat-worker

# Watch startup logs to confirm payload logging activates
docker compose logs -f --tail=20 blender-worker
```

In the logs, look for the SDK's startup INFO line:

```
INFO  task-worker-api Worker starting: id=blender-worker-1 url=http://nexus-core:5000/api/v1 types=...
INFO  payload logging: enabled, root=/app/shared/_worker_payloads/blender-worker-1, retention=14d
```

If you see `payload logging: disabled (shared_volume_path=None, env=...)`, the worker repo's entry point is missing `shared_volume_path=os.environ.get("SHARED_VOLUME_PATH")` — see the [SDK upgrade runbook's audit step](sdk-upgrade.md#audit-step-do-every-time).

## On a developer workstation

The same compose file works on Windows Docker Desktop and native Linux. The platform-specific override is:

```bash
# Linux native:
docker compose -f docker-compose.yml -f docker-compose.linux.yml up -d

# Windows Docker Desktop (default):
docker compose up -d
```

To test a specific worker repo's local changes (without going through CI):

```bash
cd /path/to/<worker-repo>
docker build -t syngular/<image-name>:dev .

# In surgiclaw:
BLENDER_WORKER_TAG=dev docker compose up -d --no-deps blender-worker
# (substitute the matching *_TAG env var from workers.json)
```

## Pinning a specific tag for prod

CI publishes both `:latest` and a dated tag (e.g., `2026.04.26-1430`). For prod, set the tag explicitly in `surgiclaw/.env` so deploys are reproducible:

```bash
# surgiclaw/.env
BLENDER_WORKER_TAG=2026.04.26-1430
COLMAP_SPLAT_WORKER_TAG=2026.04.26-1430
NEURAL_CANVAS_TAG=2026.04.26-1430
```

The image tags published per worker repo are visible at:

```bash
gh api /orgs/SyngularXR/packages/container/<image-name>/versions \
  --jq '.[] | {tag: .metadata.container.tags[0], created: .created_at}' | head -20
```

(Substitute `neural-canvas`, `blender-worker`, `colmap-splat-worker` for `<image-name>`.)

## Verifying the new SDK is in effect

Inside any worker container, after a successful claim cycle:

```bash
docker exec blender-worker ls -la /app/shared/_worker_payloads/blender-worker-1/
# Should show payloads-DATE-pidPID-BOOT.jsonl files

docker exec blender-worker python3 -c "import task_worker_api; print(task_worker_api.__version__)"
# Should print the SDK version pinned in this worker's pin file
```

If the version doesn't match what's in `workers.json` for this worker, the image needs rebuilding (CI may have failed silently — check the worker repo's Actions tab).

## Common gotchas

- **`docker compose pull` reports "no changes":** the registry might still be uploading the new push. Wait 1–2 min and retry, or check CI logs.
- **Worker comes up but doesn't claim tasks:** verify `WORKER_API_KEY` matches an entry in `WORKER_API_KEYS` on `nexus-core`, scoped to the right task types.
- **`payload logging: disabled` in startup log:** the worker's `Worker(...)` constructor isn't passing `shared_volume_path`. Open a follow-up PR on that worker repo (see audit step in the SDK upgrade runbook).
- **`raw_envelopes-*.jsonl` shows up suddenly:** a backend deploy added a new `task_type` int that the worker SDK doesn't know about — protocol drift. Bump the worker repo's `task-worker-api` dep and rebuild.
