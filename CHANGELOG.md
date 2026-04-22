# Changelog

## v0.1.0 — 2026-04-22

Initial scaffold. Ships the two schemas needed to unblock
`detect_cut_planes` end-to-end on SynPusher:

- `TaskType`, `TaskStatus` enums
- `TaskParamsBase` (with `extra="forbid"`)
- `DetectCutPlanesParams`, `ModelInitializingParams`
- `TASK_PARAMS_SCHEMAS` registry (partial — other task types land in v0.2+)
- `TaskCancelled`, `TaskParamsError`, `ProtocolError` error classes

The worker HTTP client, cancel patterns, file transfer, progress
reporter, `Worker` class, hybrid-mode runner, and testing fixtures
all land in subsequent releases as the migration phases in the
[design spec](https://github.com/SyngularXR/SynPusher-Vue/blob/main/docs/specs/2026-04-22-unified-task-queue-api-contract-design.md)
progress.
