# Worker quality and defect review ledger

Read the [workflow standard](workflow-standard.md), [improvement scopes](improvement-scopes.md)
and [owning worker guide](README.md#worker-workflow-guides) first. This index
records cross-worker review coverage; detailed findings and candidate decisions
belong in the affected worker's ledger. Review dates are UTC. Tracking's separate
model-research review is not duplicated here.

## 2026-10-19

Canonical standard, scopes and workflow index refreshed at
`7bf33260eaacc85ecaae08b5246f067c160fff10`. Worker guides/source and active Fleet
cards/PRs were reviewed before selecting the Neural-Canvas geometry regression.
Tracking's separate model-research program was not repeated.

| Operational role | Reviewed main source | New review evidence / disposition |
|---|---|---|
| Blender | `6b26e3973cc24a0c9ba43282172bcfddeb268fd3` | Source import-validation and AO callback work has landed; do not duplicate those changes. bpy range remains >=5.1,<6 with Python 3.13, PartUV 0.1.2 and SDK dev47. No newly measured bake/UV or final visual improvement from this review. |
| Gaussian / colmap-splat | `43f0801d28373174c33f6d3707e208378b98c5fa` | Recent source includes ordered view loading, scene-preparation concurrency and cancellation fixes. Background handling and worst-view metrics already have Fleet work; they are not new proposals here. COLMAP/pycolmap 4.2.1, gsplat 1.5.3, PPISP v1.3.0 and torch 2.14.1 release checks do not change the prior [worker ledger](https://github.com/SyngularXR/colmap-splat/blob/main/docs/review-ledger.md) decisions or establish worker/device benefit. |
| Neural-Canvas | `1b367ed0b5b43622d47c6839cfb2de56ddc7d080` | [PR 356](https://github.com/SyngularXR/Neural-Canvas/pull/356) fixes synthetic CT input preflight accepting NaN spatial origins with finite voxels. Three X/Y/Z NIfTI fixtures fail on baseline and pass before decoding with the shared guard. The [owning ledger](https://github.com/SyngularXR/Neural-Canvas/blob/main/docs/guide/worker-review-ledger.md) records tests, current MONAI advisories and separately observed running hybrid versions. Dedicated CT source pins SDK dev60/SimpleITK 2.5.2; shared source uses SDK dev61. No framework/weight upgrade. |
| Backend compute | `b2b8435c48f193f44d4223c3de316b3d3ebd475d` | Current guide includes optional MRI enhancement with native-sample preservation and staged budgets. Source now pins SDK dev61. Existing RAM2/native-plane and synthetic CT work remains separate from this review's input guard. No renderer/runtime upgrade or generation-speed claim. |
| Trusted backend finalizers | `b2b8435c48f193f44d4223c3de316b3d3ebd475d` | Guide includes finalize_volume/finalize_ct and distinguishes retired bone publication. Source ownership, geometry and durable recovery contracts remain the reference; no live-data mutation or newly reproduced publisher defect in this review. |
| Unity AssetBundle | `d151ee6738ae0302a928f698363d3ff60939acc3` | Project/editor source moved to 6000.3.25f1 in PR 122 to match Syngar-ML; worker SDK remains dev47. Target/platform mismatch validation is already merged. These facts do not establish a deployed Android client version, bundle loading or frame-time improvement. |
| Synthetic reconstruction | `786db308b98251a2dbaa9d964ec1f0974ff5839e` | CLI/legacy solver separation and the previous input-preservation fix remain. The new source change moves CI runners; it is not a solver/library upgrade. NumPy 2.5.4, SimpleITK 2.5.6 and NiftyMIC release checks do not change the prior [worker ledger](https://github.com/SyngularXR/synthetic-generator/blob/main/docs/review-ledger.md) compatibility decisions. |

Official checks on 2026-10-19: [COLMAP releases](https://github.com/colmap/colmap/releases),
[gsplat releases](https://github.com/nerfstudio-project/gsplat/releases),
[PPISP tags](https://github.com/nv-tlabs/ppisp/tags),
[torch releases](https://github.com/pytorch/pytorch/releases),
[bpy metadata](https://pypi.org/pypi/bpy/json) (5.2.2, uploaded 2026-09-15),
[xatlas metadata](https://pypi.org/pypi/xatlas/json) (0.0.11, uploaded 2025-07-04),
[Unity 6000.3.25f1](https://unity.com/releases/editor/whats-new/6000.3.25f1)
(published 2026-09-24), [NumPy releases](https://github.com/numpy/numpy/releases),
[SimpleITK releases](https://github.com/SimpleITK/SimpleITK/releases) and
[legacy NiftyMIC releases](https://github.com/gift-surg/NiftyMIC/releases).
No new measured library benefit was adopted.

Neural PR 356 merged as `3fbc7e8afca8adeb0953c55563ab025612d13374` after
current-head lint/format checks and the Linux suite passed (968 passed, 7 skipped).
Local validation also passed 37 focused tests with the current shared-source SDK.

New MONAI advisories dated 2026-10-13/15 were checked against used entry points;
VISTA3D already passes `logging_file: False`, and no worker/script call to
`MonaiAlgo` or `algo_from_json` was found. The owning Neural ledger records the
official URLs, decisions and compatibility/benchmark gates rather than claiming
an exposed exploit or a safe blanket framework upgrade. Older open wheel/OpenCV
alerts have existing dependency PRs and are not duplicated here.

Read-only Orttu evidence: running hybrid Neural image digest/package metadata
and a bounded recent log-signature sample are retained in the Neural ledger.
They do not identify the dedicated CT image/profile/weights or deployed Android
client. Other worker deployment identities, resource profiles, final-consumer
visuals, Android loading/memory/frame-time tails and generation-speed changes
were not measured. No GPU benchmark, admission bypass or live deployment occurred.

The focused change adds early input refusal; no model, effective settings,
dependency pin or output format changes. Any future PPISP/framework/weight
comparison still needs retained same-input final artifacts and the shared visual
and Android gates. Next deeper rotation: Unity packaging/publication evidence or
an admitted Gaussian candidate comparison when original inputs/device evidence
are available.

## 2026-10-12

Canonical SDK context reviewed at `0ba3ced823d028c9af3cea5e36e332866c07775b`.
Fresh worker main refs were read independently of dirty local checkouts. Existing
Fleet cards/PRs were checked to avoid duplicating active work. No managed CPU/GPU
benchmark was launched outside admission and no live deployment was performed.

| Operational role | Reviewed main source | Review / decision |
|---|---|---|
| Blender | `9de0f81a39a59ba5833435c1f4a28cdb36b0f1ec` | Model/UV/bake guide and dependency/image sources reviewed. Existing Fleet geometry/bake fixes and open PR 159 remain separate. Python 3.13, `bpy>=5.1,<6`, PartUV 0.1.2 and xatlas range describe source requirements, not resolved/deployed versions. |
| Gaussian / colmap-splat | `cdc98d6c79bb4bba2640b614396b8e39161d4145` | [Worker ledger](https://github.com/SyngularXR/colmap-splat/blob/main/docs/review-ledger.md): PPISP v1.3.0 and CUDA compiler correctness candidates, COLMAP/pycolmap 4.2.1 deferred, gsplat 1.5.3 already pinned. [PR 98](https://github.com/SyngularXR/colmap-splat/pull/98) records compatibility and fair comparison gates; no runtime upgrade. Existing schedule/SSIM work is separate. |
| Neural-Canvas | `a6c582693d94d1301ff2369a0531a9b8da45de09` | Segmentation/spatial/dispatch guide, requirements and VISTA3D bundle runner reviewed. MONAI 1.6.0, SimpleITK 2.4.1 and SDK dev47 are source pins. CUDA 12.8 Docker installs torch without an exact version; resolved image packages/weights require inspection. See upstream review below; no model or framework change. |
| Backend compute | `671618dca9a8f483501e9ba74fffafa65e475588` | Render, GS4D and deploy-preparation guide/source requirements reviewed. SDK archive ref `17d7e481aba4d557432253c189417de75a995954` is authoritative for this source; do not infer loaded SDK or Spectra runtime from a guide's older ref. No new measured output/speed candidate selected. |
| Trusted backend finalizers | `671618dca9a8f483501e9ba74fffafa65e475588` | Ten publisher routes and trusted CPU ownership/recovery guide reviewed against requirements. Shares the backend SDK ref above. Existing Fleet publication/recovery work remains separate; no live-data mutation or newly reproduced finalizer defect in this review. |
| Unity AssetBundle | `760fe6e364b52e824f424fd3ea6f59b742de8ee8` | Build/cache/licensing/publication guide and project/image pins reviewed: editor 6000.3.10f1. Existing cache/import-content and license/finalization timing work remains separate. See upstream review below; no editor/shader/output change. |
| Synthetic reconstruction | `79b86446930c8c7dbe83385e9ea11bf411b9f0c5` | [Worker ledger](https://github.com/SyngularXR/synthetic-generator/blob/main/docs/review-ledger.md): six reproduced cleanup collisions lost original input files; [PR 51](https://github.com/SyngularXR/synthetic-generator/pull/51) protects them before cleanup/computation. Six baseline failures became six passing regressions; valid full/ROI contracts pass. CLI and legacy NiftyMIC reviewed separately; no solver/pin change. |

### Other official upstream checks

| Evidence, date and revision | Actual path / compatibility | Decision and next evidence |
|---|---|---|
| [Blender source releases](https://download.blender.org/source/): 5.2.2 published 2026-09-15; reviewed 2026-10-12 | The worker's bpy range already permits 5.2.x; model processing and Cycles baking use bpy. A release archive does not prove which bpy wheel was resolved. | No upgrade selected or speed claim. Inspect actual bpy/driver/mesh-tool versions first; any change needs the same original thin-triangle/UV/bake fixtures, final mesh/textures and actual-consumer/Android comparison. |
| [MONAI 1.6.1](https://github.com/Project-MONAI/MONAI/releases/tag/1.6.1), published 2026-09-27; [bundle advisory](https://github.com/Project-MONAI/MONAI/security/advisories/GHSA-873f-pvrv-4x83), published 2026-08-21 | VISTA3D's `src/modules/segment/vista3d/runner.py` calls `monai.bundle.run` with config/model-root paths supplied by server runtime configuration. Untrusted bundle execution is the advisory's condition; no attacker-controlled bundle route was reproduced. A framework patch does not make arbitrary bundle code trusted. | Candidate for a focused trust/compatibility review, not an automatic bump. Establish bundle/checkpoint provenance and directory ownership; compare fixed scans/weights, label geometry and final masks, subprocess cancellation/memory and actual consumer/Android views before any output-affecting adoption. Release loss fixes alone do not prove inference benefit. |
| [Unity 6000.3.0f1 notes](https://unity.com/releases/editor/whats-new/6000.3.0f1), published 2025-12-03; [6000.3.17f1 notes](https://unity.com/releases/editor/whats-new/6000.3.17f1), published 2026-06-04 | The worker is already on 6000.3.10f1, beyond the .0 AssetBundle type-tree memory-sharing improvement. The .17 Android keyboard/navigation fix concerns the player, not evidence of faster bundle generation. These are checked revisions, not a claim about the newest channel. | Rejected as demonstrated worker upgrade benefits. Any editor/shader candidate requires matching deployed Android client/serialization/shader support and fixed-snapshot build/cache timings, bundle loading/memory plus matched close-ups/motion/frame-time tails. |

### Evidence boundary and next rotation

PR 51's complete Linux suite passed with and without the SDK before merge;
PR 98's unit/shell checks and CUDA image build passed before merge. Their owning
guides link the new ledgers. These checks establish the recorded regression and
documentation changes, not reconstruction/training quality or device speed.

Deployed image digests, loaded SDKs, supervisor resource profiles and Android
device/client versions were not inspected. Original-input benchmark artifacts,
consumer visual comparisons, motion, loading/memory/frame-time tails/hitches and
stage/end-to-end speed improvements were not measured. None is inferred from
training loss, upstream timings, smaller files or desktop rendering.

Next deeper investigation: PPISP's used forward/backward path if original inputs
and admitted comparison capacity are available; otherwise VISTA3D bundle/weight
provenance and framework compatibility. Preserve baseline/candidate final outputs,
fix effective settings/cache state, distinguish queue/admission costs, and reject
visual degradation before merging an output-affecting change.
