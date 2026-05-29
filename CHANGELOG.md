# Changelog

All notable changes to swm are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.5] - 2026-05-29

### Fixed
- Framework SSH tunnels (e.g. `swm setup start comfyui`) now use the
  configured SSH identity. `_open_tunnel` built a bare `ssh` command without
  `-i <key_path>`, so when a custom `ssh.key_path` (or `<provider>.ssh_key`)
  was set it silently fell back to the default key, failed authentication, and
  — because of `ExitOnForwardFailure=yes` with output discarded — exited
  immediately without forwarding the port. The tunnel now resolves its
  host/port/user/key via the same path as `swm run`/`swm ssh`, so it also
  targets the direct IP and mapped port-22 endpoint when available.

## [0.2.4] - 2026-05-27

### Fixed
- `swm sync push --force` now propagates the s5cmd exit code through the CLI;
  previously partial failures (e.g. B2 503 SlowDown on a single object) were
  silently reported as success.
- `swm sync push --force` advances the push stamp even on partial s5cmd
  failure during the initial baseline upload, so the next autosync cycle can
  retry missed files via `find -newer` instead of permanently refusing to start.
- `swm sync auto --force` touches the push stamp on the pod when missing,
  matching the client-side `--force` semantics so the daemon's internal stamp
  check no longer blocks startup after a forced push.
- `swm pod create -n <name>` (without `-w`) no longer fails when `/workspace`
  contains files from the docker image — `swm` now uploads the existing
  contents as the new workspace baseline and starts autosync normally. This
  was the root cause of silent data loss when a pod was created without `-w`.

### Added
- `swm pod status <pod>` now reports autosync running state, watcher state,
  last push age, and pending change count over SSH. Loudly warns when
  autosync is not running on a pod that has been up > 5 minutes.
- `swm pod down --force-down` flag to override the new sync-safety guard
  (terminate even if the workspace push did not write a recent stamp).

### Changed
- `swm pod down` refuses to wipe `/workspace` or terminate the pod when the
  workspace push exits non-zero or fails to write a push stamp within the
  last 10 minutes. The lifecycle guard's `auto-down` path applies the same
  guard and leaves the pod alive on partial-push failure.

## [0.2.3] - 2026-05-24

### Fixed
- `swm sync pull --tar` no longer reports success when download/extract fails.
- `swm models pull` tracks each HF file under a unique manifest key; multi-file
  repos (e.g. high/low LoRA pairs) no longer overwrite each other.
- HF pull 404s suggest nested repo paths; `--filename` resolves basenames when unique.
- `swm guard set` honors `--idle-timeout 0` / `--poll-interval 0` and starts the
  local guard daemon for auto-stop/auto-down policies.
- `swm sync status` reports push stamp, watcher, pending changes, and auto-sync.
- `swm use` validates the pod exists before setting the active pod.
- SwarmUI install creates `dlbackend` before cloning ComfyUI (fixes #9).
- `swm gpus -p badslug` errors instead of returning empty results.
- `swm costs summary --period today` uses UTC calendar day, not rolling 24h.
- Cost tracking failures on pod lifecycle commands emit dim warnings.

### Changed
- Removed deprecated shims: `swm models set`, `swm pod gpus`, hidden `setup comfyui` /
  `setup swarmui`.
- `swm guard disable --force-manual` pins a pod to manual mode.
- `swm pricing` supports H100, A100, RTX 4090, and L40S reference data.
- Shell completion suggests `provider:id` pod references.
- Config masking uses suffix-based sensitive key detection.
- Azure optional dependencies in `pyproject.toml` (`pip install swm-gpu[azure]`).
- Regenerated repo docs (`docs/cli-reference.md`, `docs/configuration.md`).

## [0.2.2] - 2026-05-24

### Added
- Workspace-owned Python toolchain under `/workspace/`: pinned `uv` binary,
  managed CPython via python-build-standalone, and `repair_venv` to rebind
  pulled venvs when the host image changes (preserves site-packages within
  the same minor).
- `Framework.venv` field so install/start automatically ensure uv + Python
  and repair stale venvs before framework steps run.
- Remotion demo video project (`video/`) with Hero 16×9, vertical 9×16, and
  square 1×1 compositions; scrubbed terminal script and brand tokens from the
  site palette.
- Site hero demo assets (`site/public/demo/hero.mp4`, `hero-poster.jpg`).

### Changed
- ComfyUI, vLLM, Open WebUI, Axolotl, and H2O LLM Studio now create venvs
  with `uv venv --python 3.11` and install packages via `uv pip` instead of
  host `python3 -m venv` / bundled pip (drops ComfyUI get-pip repair path).
- Open WebUI no longer installs uv via host `pip3`; uses workspace-local uv.

## [0.2.1] - 2026-05-22

### Added
- `swm setup start --extra-args` appends extra launch flags to any framework
  (e.g. ComfyUI `--use-sage-attention`).

### Fixed
- Workspace sync push/autosync now reconciles inotify events with a
  high-watermark `find -newer` scan so fast file bursts are not missed.
- `swm sync pull` re-runs idempotent framework link/symlink repair steps
  after restore so ComfyUI/vLLM/Ollama paths into `/workspace/models/`
  stay wired.
- `swm models pull` quotes Civitai/URL destination paths and sanitizes
  Civitai filenames with shell-special characters.
- `swm models pull` with a URL respects `--as` for bucket routing.
- ComfyUI pre-start no longer deletes the venv or force-reinstalls pip;
  missing venv now errors with a clear re-install hint.
- `scp` upload/download no longer consumes stdin (fixes hangs in piped
  agent contexts).

### Changed
- Documentation and agent skill updated for the unified model store and
  removal of `swm models set`.

## [0.2.0] - 2026-04-03

### Added
- Unified on-pod model store at `/workspace/models/` with per-asset-type
  buckets (`checkpoints/`, `loras/`, `vae/`, `controlnet/`, `embeddings/`,
  `clip/`, `clip_vision/`, `upscale_models/`, `unet/`, `diffusion_models/`,
  `text_encoders/`, `hf/`, `ollama/`, `files/`). Framework installs wire
  their expected paths into this store via bucket-style symlinks, so models
  are visible to all frameworks (vLLM, Ollama, ComfyUI, SwarmUI) without
  per-file linking.
- `swm models pull` now supports HuggingFace, Ollama, Civitai, and direct
  URL sources. The reference shape auto-detects the source; pass
  `--source hf|ollama|civitai|url` to override. Ambiguous single-segment
  refs trigger a parallel HF + Ollama registry lookup with a prompt on
  dual hits.
- `swm models pull --as <type>` files the download into the right bucket.
  Defaults are inferred from HF metadata (pipeline, library) or Civitai's
  asset type field.
- `swm models link <pod> <path> --as <type>`: register an existing on-pod
  file under the unified store. Moves the file into the right bucket and
  adds a manifest entry so `swm models list` knows about it.
- `swm models list --all`: surface untracked files under `/workspace/models/`
  so they can be `swm models link`'d.
- Civitai support: `swm config set civitai.api_key <token>`,
  `swm models info civitai:<id>`, `swm models pull <pod>
  civitai:<id>[:<version>]`. NSFW and gated content honor your API key.
- HuggingFace API key now follows the GPU-provider pattern:
  `swm config set hf.api_key <token>`. The legacy `hf_token` config key
  is still read but emits a deprecation warning.
- `swm setup start vllm <pod> --model <ref>` writes `/workspace/vllm/model.txt`
  before launching, replacing the `swm models set` workflow.
- Best-effort engine fallbacks: pulling an Ollama-shape reference on a pod
  without the Ollama binary now searches HuggingFace for a `bartowski/*-GGUF`
  mirror and pulls that as a single-file download, with a friendly install
  reminder. HF pulls succeed even when vLLM isn't installed yet.
- Persistent manifest of every download at `/workspace/models/.manifest.json`
  drives reconciliation in `swm models list` (tracked vs missing vs orphan).

### Changed
- ComfyUI install now wires the bucket-style symlinks during `post_install`
  instead of relying on the hidden `swm setup comfyui` alias. Anyone who
  previously ran `swm setup install comfyui` and noticed missing model
  visibility from `swm models pull` gets it for free on the next install
  or `swm setup start`.
- vLLM's `/workspace/vllm/hf_cache` and Ollama's `/workspace/ollama/models`
  are now symlinks into the unified store. Existing content is migrated
  in place — no data loss for upgrading pods.

### Removed
- `swm models set`. A deprecation shim still routes the command but errors
  out with the new equivalent. Will be deleted in v0.3.

## [0.1.13] - 2026-05-20

### Changed
- Pruned dead code (`build.sh`, the unused `b2` Python SDK extra) and
  regenerated `docs/` (`cli-reference.md`, `configuration.md`,
  `architecture.md`, `storage.md`) to match the v0.1.12 CLI surface.

## [0.1.12] - 2026-05-20

### Added
- `safe_resolve_instance` helper centralizes instance lookups. Stale
  active-pod entries now produce a friendly `click.UsageError` plus an
  auto-clear, instead of a raw `ValueError` traceback.

### Fixed
- `swm models search` column truncation on narrow terminals.
- `clear_active_pod` no longer silently fails on type mismatch between
  config (int) and lookup (str).

### Removed
- `swm security` command stub ("Coming soon — Phase 9").

## [0.1.11] - 2026-05-20

### Fixed
- `swm pod down` no longer kills the `inotify` watcher mid-flow, so the
  pre-terminate `workspace_push` stays on the Tier 1 fast path.

### Added
- SWM wordmark favicon + homepage demo video on swmgpu.com.

## [0.1.10] - 2026-05-11

### Added
- Custom S3 endpoint URL support for workspace sync. Backblaze B2 remains the
  default, but Cloudflare R2, MinIO, and other S3-compatible stores now work
  via `--s3-endpoint-url`. Thanks @hirokazu (#1).
- Dependabot configuration for GitHub Actions, pip, and Homebrew formula
  dependencies.
- `CODEOWNERS` and `SECURITY.md`.

### Changed
- Bumped `actions/checkout` (4 → 6), `actions/download-artifact` (4 → 8), and
  `softprops/action-gh-release` (2 → 3) via Dependabot PRs.

## [0.1.9] - 2026-05-10

### Fixed
- `swm pod create -p vastai` now honors the `--disk` and `--volume` flags
  instead of silently overriding them with provider defaults.

## [0.1.8] - 2026-05-04

### Fixed
- Brew binary is now `chmod 0555` before `generate_completions_from_executable`
  runs, fixing a Homebrew formula audit failure on macOS Sequoia runners.

## [0.1.7] - 2026-05-04

### Added
- Working shell autocomplete (bash, zsh, fish) shipped via Homebrew and
  documented in the install guide.

## [0.1.6] - 2026-04-28

### Fixed
- `swm gpus -p vastai` was missing H100, H200, and B200 datacenter SKUs due to
  a category filter that excluded them by default.

## [0.1.5] - 2026-04-27

### Added
- Multi-stage code audit pass: 6 bug fixes, 8 dead-code removals across 17
  files. See the release notes on GitHub for the full breakdown.

### Fixed
- Vast.ai `runtype` typo (`ssh_direc` → `ssh_direct`) that blocked pod
  creation against new Vast.ai instances.
- `guard.py` crash when `inst.cost_per_hr` is `None`.
- Autosync daemon now re-appends the snapshot to `WATCH_LOG` on `s5cmd`
  failure instead of silently dropping the change.
- `swm guard` now honors explicit `0` for idle / max-cost thresholds instead
  of falling back to defaults.

## [0.1.4] - 2026-04-27

### Added
- Initial Homebrew tap (`swm-gpu/homebrew-swm`) with prebuilt binaries for
  darwin-arm64, darwin-amd64, and linux-amd64.
- GitHub release workflow publishing to PyPI and Homebrew on tag push.

## [0.1.0] - 2026-04-26

### Added
- First public release. Search and provisioning across RunPod, Vast.ai,
  Lambda Labs, Vultr, TensorDock, FluidStack, AWS, GCP, Azure, and CoreWeave.
  Lifecycle guard, workspace sync (Backblaze B2), and framework installers
  for vLLM, ComfyUI, SwarmUI, Open WebUI, Axolotl, Ollama, and H2O LLM Studio.

[Unreleased]: https://github.com/swm-gpu/swm/compare/v0.1.10...HEAD
[0.1.10]: https://github.com/swm-gpu/swm/compare/v0.1.9...v0.1.10
[0.1.9]: https://github.com/swm-gpu/swm/compare/v0.1.8...v0.1.9
[0.1.8]: https://github.com/swm-gpu/swm/compare/v0.1.7...v0.1.8
[0.1.7]: https://github.com/swm-gpu/swm/compare/v0.1.6...v0.1.7
[0.1.6]: https://github.com/swm-gpu/swm/compare/v0.1.5...v0.1.6
[0.1.5]: https://github.com/swm-gpu/swm/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/swm-gpu/swm/compare/v0.1.0...v0.1.4
[0.1.0]: https://github.com/swm-gpu/swm/releases/tag/v0.1.0
