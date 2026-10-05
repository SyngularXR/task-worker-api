# Worker improvement scopes

This is the shared acceptance contract for worker quality, real-time rendering,
generation speed and bug fixes. Read the [workflow standard](workflow-standard.md),
the worker's [workflow guide](README.md#worker-workflow-guides), then the linked
implementation. These are investigation scopes, not claims that defects were
reproduced, upgrades are faster, or the deployed fleet has passed these checks.

The seven `s4-*` names below identify operational roles from the fleet view.
Verify current enrollment, task routing, source revision, image digest and SDK
pin on the target host before running an experiment. The inventory alone does
not establish them. Tracking is an additional foundation, not a deployed worker.

## Shared acceptance gates

Quality has three dimensions: final visual fidelity, real-time client rendering
and generation cost. A speed or size improvement must preserve visual quality.
Rendering performance must be checked on the supported target device when an
output affects the 3D client; generation speed is a separate measurement.

1. **Freeze a reproducible baseline.** Use the same original input hashes, task
   parameters, seeds where supported, source/image/dependency/model-weight
   versions, hardware and effective settings. Record differences deliberately.
   Retain baseline and candidate final artifacts and the commands/task IDs.
2. **Compare final outputs visually.** Use matched cameras and motion paths,
   lighting, exposure, color space, materials, scale and renderer settings in
   the actual consumer. Include close-ups, silhouettes, thin features, cavities,
   text, UV seams, color/shading, transparency, occlusion and motion artifacts as
   applicable. Check stereo when the supported client uses it. Attach paired
   screenshots and matched clips; include original/reference data where useful.
   Geometry or image metrics support review, but do not replace it. Training
   loss alone cannot prove exported Gaussian quality. Reject visual degradation.
3. **Measure real-time cost.** On a named supported Android device and client
   build, record CPU/GPU frame times (median and tail), hitches/dropped frames,
   load/upload time, peak memory and artifact bytes. Record thermal state,
   resolution and scene contents; distinguish cold load from warmed rendering.
   Record triangles, splats, texture budgets or draw calls when they explain a
   change. Desktop rendering is not proof of Android performance.
4. **Measure generation cost.** Report stage and end-to-end wall times, peak
   RAM/VRAM and disk use. Separate queue/admission, license wait, input transfer,
   cold initialization, computation, export, upload and publication. Control
   cache state. Repeat matched runs (normally at least three) and report spread;
   declare budgets, meaningful benefit and measurement tolerance before testing.
   There is no universal FPS or triangle budget for every worker and device.
5. **Reproduce and fix defects.** Capture a minimal failing input/task and the
   violated contract. Add a regression check that fails before the fix and
   passes after it. Exercise affected failure, retry and cancellation paths.
   Do not list a hypothetical edge case as a confirmed bug.
6. **Verify integration.** Check Surgiclaw admission/profile/resource ownership,
   private input/output contracts, SDK/schema pins, publication and the Android
   consumer. A child `done`, artifact commit, published case and released device
   reservation are separate facts. Use existing admitted execution for managed
   compute; deploy services only through
   [surgiclaw-deploy](https://github.com/SyngularXR/surgiclaw-deploy).

For a runtime improvement PR, attach the comparison report and update the
affected workflow guide in the same PR. Mark unavailable measurements explicitly
as **not measured**. A change affecting Android rendering is not proven safe
until that device/client comparison is complete. Do not compensate for missing
evidence with a claim that visuals or performance are unchanged. Documentation
changes alone do not require a GPU or Android benchmark.

## Worker scopes

### Blender — `s4-blender`

- Tasks: `model_initializing`, `cinematic_baking`, `detect_cut_planes`.
- Quality: preserve surfaces, thin structures, topology, scale, cut-plane
  transforms, normals, material/color and UV/bake continuity. Compare final
  meshes and baked textures in the consuming Unity/Android scene, including AO
  streaks, seams and small details. Existing artifacts may have defects; keep
  the original high-quality source alongside the baseline comparison.
- Speed: measure import, geometry processing, Part UV, each bake and export.
  Compare any thin-triangle merge, simplification, UV or texture-budget change
  against the same original model. Lower polygon or bake resolution is not an
  accepted improvement without visual and client-rendering evidence.
- Bugs/integration: empty/degenerate meshes, sliver triangles, non-finite
  coordinates, multi-part models, unusual units, large textures, memory limits,
  child cancellation, partial bake outputs and backend model publication.
- Dependencies: Blender, mesh/UV tools, bake kernels and SDK; verify file-format
  and material behavior across the worker and Unity importer.

### Gaussian builds — `s4-gs` / colmap-splat

- Tasks: `gs_build`, `spatial_gs_build`.
- Quality: held-out views plus exported splats in the real renderer; inspect
  detail, colors/exposure, holes, floaters, opacity, view-dependent popping,
  coordinate alignment and crop boundaries. Compare dense and sparse cases.
- Speed: separate COLMAP feature extraction/matching/mapping, optional dense
  reconstruction, cache validation, training and export. Compare splat caps,
  sampling/densification, PPISP and CUDA changes with fixed inputs and budgets.
  Faster training does not establish a faster Android renderer.
- Bugs/integration: zero/one usable view, weak overlap, failed registration,
  disconnected reconstructions, wrong intrinsics/scale, malformed or stale
  caches, non-finite PLY data, OOM, cancellation and interrupted export; verify
  spatial and ordinary GS publication and Unity shader/PLY expectations.
- Dependencies: COLMAP/pycolmap, gsplat, PPISP, PyTorch/CUDA and matching models.
  Keep ABI/runtime compatibility and actual exercised command paths explicit.

### Neural-Canvas — `s4-neural`

- Tasks: `segmentation`, conditionally enabled `spatial_recon`; its synthetic
  dispatch route is distinct from standalone reconstruction.
- Quality: compare masks at boundaries and thin structures, connected
  components, labels and generated surfaces; compare spatial geometry, depth,
  camera poses and texture alignment in the client. Include representative
  difficult input and ground truth when available.
- Speed: isolate model/weight loading, inference, preprocessing, meshing and
  artifact writes. Preserve accuracy when changing precision, batching or
  model versions; report cold and cached behavior separately.
- Bugs/integration: empty/all-foreground masks, unexpected dimensions, image
  orientation/spacing, missing weights, unavailable optional dependencies,
  invalid captures/poses, output identity, cancellation and nested dispatch
  failures; test backend segment/spatial finalizers and Android display.
- Dependencies: inference framework, segmentation/reconstruction models,
  image/mesh libraries, SDK and weight checksums/licenses.

### Backend compute — `s4-backend-compute`

- Tasks: `render`, `gs4d_build`, `prepare_deploy` in
  [SynPusher-Vue backend services](https://github.com/SyngularXR/SynPusher-Vue/tree/main/services/backend/src/services).
- Quality: verify rendered cameras, color/exposure, volume orientation and
  COLMAP inputs; GS4D phase ordering, transforms and temporal stability; deploy
  snapshots preserve every required asset and its metadata. GS4D preparation
  and assembly are separate from downstream rendering and Gaussian training.
- Speed: measure renderer startup/cameras, checkpoint transfer, per-phase
  preparation, sequence assembly and deploy snapshot copies separately.
- Bugs/integration: invalid frozen paths, camera/checkpoint mismatch, partial
  resume, missing frame/phase, wrong coordinate system, incomplete snapshot,
  shared-file mutations, cancellation, disk pressure and atomic handoffs to
  trusted finalizers. Verify Android phase playback and packaged scene loading.
- Dependencies: Spectra/render kernels, volume IO, sequence/PLY tooling and SDK.

### Backend publication — `s4-backend-finalizer`

- Tasks: `finalize_cinematic`, `finalize_deploy`, `finalize_deploy_prep`,
  `finalize_gs`, `finalize_gs4d`, `finalize_model`, `finalize_render`,
  `finalize_segment`, `finalize_spatial`, `finalize_synthetic`; implemented by
  [trusted backend finalizers](https://github.com/SyngularXR/SynPusher-Vue/blob/main/services/backend/src/services/resource_finalizer_worker.py).
- Quality: publication must preserve the committed artifacts, units, transforms,
  labels, intensity windows and associations. Check final client-visible data,
  not just successful worker uploads. Never silently accept incomplete outputs.
- Speed: measure validation, locking, file promotion, database updates and
  downstream task creation. Do not shorten validation to reduce latency.
- Bugs/integration: duplicate/replayed commits, expired ownership, mismatched
  input/profile/parent IDs, interrupted promotion, undo/recovery, concurrent
  case edits and ambiguous commit/follow-up creation. Verify each publisher's
  actual transaction/recovery contract; they are not interchangeable.
- Dependencies: SDK admission protocol, ORM/database, artifact validators,
  DICOM/volume/PLY libraries and Android metadata readers. Migrate live data
  when needed; never wipe it as a compatibility shortcut.

### Unity packaging — `s4-assetbundle`

- Task: `deploy_case`.
- Quality: compare the built Android bundle, materials/shaders, textures,
  colliders, transforms, GS assets and animation against source outputs in the
  supported client. Check visual parity, load/unload and missing dependencies.
- Speed: separate snapshot transfer, Unity/license wait, import, build,
  validation and publication; measure bundle bytes, load time, memory and frame
  times. Compression, texture formats and shader stripping require device QA.
- Bugs/integration: licensing contention, missing assets, stale snapshots,
  duplicate filenames/identities, platform/build-target mismatch, interrupted
  uploads, retries and SDK cancellation; verify manifest/hash and consumer
  version contracts with Surgiclaw and Android.
- Dependencies: Unity/editor modules, bundle format, GS renderer/shaders,
  compression/import tooling and SDK. An editor upgrade needs matching runtime
  compatibility and shader checks, not just a successful build.

### Synthetic reconstruction — `s4-synthetic`

- Task: `generate_synthetic` in
  [synthetic-generator](https://github.com/SyngularXR/synthetic-generator).
- Quality: compare reconstructed anatomy/detail, artifacts and intensity on
  matched orthogonal slices and 3D views; verify reference-frame ROI placement,
  coverage, voxel spacing/origin/direction and windowing after DICOM publication.
  Finer output spacing alone does not prove recovered detail.
- Speed: separate source/ROI/mask preparation, NiftyMIC reconstruction,
  resampling/paste, write and finalization; include voxel count and peak memory.
- Bugs/integration: incompatible or disjoint stacks, partial overlap, empty
  masks, incorrect reference size/IDs, tiny ROI, non-finite voxels, insufficient
  output disk, timeout and child-tree cancellation; verify DICOM metadata,
  published intensity windows and Android volume rendering.
- Dependencies: NiftyMIC and its pinned legacy Python/SimpleITK environment,
  isolated modern CLI environment, NumPy, volume/DICOM IO and SDK. Audit both
  environments; a blanket dependency bump can break bindings or geometry.

### Visual tracking foundation — not enrolled/deployed

- Current scope: protocol-control replay and independent benchmark evaluation;
  no real tracker is implemented. Keep control results separate from real-world
  accuracy, latency or device performance claims.
- Future algorithm comparisons must use identical camera/pose sequences and
  reference trajectories, tracking loss/relocalization, drift, jitter and
  end-to-end latency in the actual client. Frame identity, timestamps, coordinate
  conventions, malformed input and cancellation belong in regression checks.
- Review SDK and benchmark tooling now; assess tracker/model dependencies when
  a real implementation exists. Do not enroll a foundation by documenting it.

## Common defect and integration matrix

Choose applicable cases for the changed route and record pass/fail/not tested:

| Boundary | Cases to exercise |
|---|---|
| Input | Empty/corrupt/missing/non-finite data, duplicate IDs, dimensions/units/poses, minimum and maximum budgets, path traversal/symlinks |
| Execution | Cold/cache behavior, OOM, full disk, concurrent jobs, timeout, cancellation, lease expiry and process shutdown |
| Handoff | Missing/partial artifacts, digest/profile mismatch, stale cache, retry, interrupted upload, ambiguous commit and restart recovery |
| Publication | Ownership checks, duplicate outcomes, undo/recovery, case edits, parent/child task dependencies and resource release |
| Android | Artifact/parser/shader versions, coordinate alignment, load/unload/reload, peak memory, thermal behavior and rendering the final published asset |

## Regular upstream review

Review official release notes and security/bug advisories regularly and before
an affected improvement. Record the deployed version separately from repository
pins. Include source URL/date, candidate version/commit, relevant code path,
expected benefit, compatibility changes and a benchmark plan. A release being
newer is insufficient evidence to upgrade. Check transitive dependencies,
Python/NumPy/torch/CUDA/compiler ABI, model weights/licenses, output formats and
client compatibility. Pin the chosen revision/image and update the workflow
and deployment configuration together where required.

Initial review candidates (2026-10-05; untested on this fleet):

- [COLMAP 4.2.1](https://github.com/colmap/colmap/releases/tag/4.2.1) includes
  reconstruction, file/geometry and ABI fixes. The colmap-splat source pins
  pycolmap 4.2.0; inspect the separate container COLMAP binary version first.
  Reproduce relevant fixes and benchmark the commands actually used. LoMa or
  global-mapper fixes do not establish benefit for a SIFT/exhaustive/mapper run.
- [gsplat releases](https://github.com/nerfstudio-project/gsplat/releases) list
  v1.5.3 as the latest tagged release at review time; colmap-splat already pins
  1.5.3. Do not claim an available tagged upgrade or measured speed gain.

Keep a review ledger in the affected worker repo, linked from its guide, when a
candidate is evaluated. Record rejected/no-benefit candidates as well as adopted
ones so agents do not repeat an unsupported upgrade proposal.

## Comparison report template

Use in a benchmark report or runtime PR; link retained artifacts instead of
embedding large binaries or sensitive case data in Git.

| Field | Baseline | Candidate |
|---|---|---|
| Input hash/task IDs; effective params and seeds | | |
| Source/image/SDK/library/weight versions | | |
| Worker hardware; cache state; repeated runs | | |
| Stage and end-to-end time; RAM/VRAM/disk | | |
| Final output hashes, bytes and geometry/splat/texture budgets | | |
| Matched visual screenshots/clips and reference data | | |
| Android device/client/scene/render settings | | |
| Frame-time median/tails, hitches, load time and memory | | |
| Regression reproduction and relevant integration checks | | |

State the visual review decision, measured benefit and run-to-run variation,
remaining limitations, untested boundaries and rollback/migration plan. Attach
official upstream references for dependency changes. Update the stage diagram
and effective settings after selecting an improvement, not before measuring it.
