# Changelog

All notable changes to swm are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.14] - 2026-08-18

### Added
- **Frameworks now say how to use them, not just that they started.** Starting
  Ollama ended at a URL whose page reads "Ollama is running" and nothing else:
  the registry knew every port but nothing about what answers on it, so every
  client could only offer "open the URL" — right for ComfyUI, a dead end for an
  API server. Each framework now declares `access` (`ui`, `api`, or `none`) and
  a list of `Usage` entries: labelled command templates taking `{base_url}` and
  `{model}`. `render_usage()` materializes them against whatever endpoint the
  client routes through (host:port for the CLI, a tunnel URL on swm.cloud) and
  drops entries whose model is not configured, since a snippet that cannot run
  as pasted is worse than none. Ollama and vLLM carry curl and OpenAI-endpoint
  entries, Axolotl its SSH training commands; the web UIs keep the default of
  opening the URL. The CLI prints the entries after `setup start`. Purely
  additive — no existing field or consumer changes.

## [0.2.13] - 2026-08-18

### Fixed
- **Pods that do not log in as root could not be provisioned.** Bootstrap
  assumed a root login on a container image. RunPod, Vast.ai and Vultr do log
  in as root and ship `/workspace` in the image, so the assumption went
  unnoticed; every VM provider — Lambda Labs, FluidStack, TensorDock, Azure —
  logs in as an unprivileged user, and there `/workspace` does not exist and
  cannot be created, while `/usr/local/bin` cannot be written. The pod would
  provision, report running, and then fail installing s5cmd with "Permission
  denied", leaving a GPU that bills and cannot be used.

  The root cause was the missing directory rather than the missing `sudo`:
  everything after the first step (uv, the managed Python, framework installs)
  is already unprivileged and only needs `/workspace` to be writable. It is now
  created once and chowned to the login user, which makes one unprivileged
  bootstrap correct on both kinds of image. Only three operations genuinely
  need privilege — the s5cmd install, the `inotify-tools` install, and the
  `pigz` install — and they now ask for it.

  Escalation is always `sudo -n`, never bare `sudo`. A bare `sudo` on an image
  without a `NOPASSWD` grant blocks on stdin waiting for a password, which
  inside a provisioning job means a pod that bills until its lease expires;
  `-n` turns that into an immediate, readable failure. It is applied only when
  not already root, because container images log in as root and frequently do
  not ship `sudo` at all.

## [0.2.12] - 2026-07-27

### Security
- **Credentials no longer reach the terminal.** RunPod's GraphQL API takes
  the key as a URL query parameter rather than a header, so httpx built
  exception messages around a live credential: a failed `swm pod list`
  printed the full API key, which then persisted in shell scrollback, CI
  logs, and any pasted bug report. Output is scrubbed at the three places
  text reaches a terminal — the Rich consoles, Click's error reporter, and
  the uncaught-traceback hook — rather than at each of the ~60 raise sites.
  Redaction replaces the exact credentials found in config, including those
  injected through `config.overlay()`, and falls back to matching the shapes
  credentials travel in (`api_key=` query params, `Authorization: Bearer`
  headers, `"token": "…"` JSON bodies) for secrets that never reach config.
- **`swm config list` displayed `gcs.hmac_secret` and `gcs.hmac_access` in
  full.** Sensitive keys were matched by exact suffix — `.secret_key`,
  `.access_key` — which `hmac_secret` and `hmac_access` never matched, so
  the GCS HMAC credentials were printed unmasked on every invocation. Key
  classification now matches substrings. Path-like keys such as
  `ssh.key_path` and `aws.key_name` deliberately still display in full,
  since hiding them makes the config unreadable for no security benefit.

## [0.2.11] - 2026-07-27

### Fixed
- **Vast.ai instance listing migrated to the v1 API.** Vast.ai deprecated
  `GET /api/v0/instances/`, which now returns `410 Gone` with
  `{"error":"deprecated_endpoint"}` for accounts included in the staged
  rollout, breaking `swm pod list`, `swm pod status`, workspace sync, and
  every command that resolves a pod by name. Listing now uses
  `GET /api/v1/instances/` with keyset pagination. The v1 rows are
  field-identical to v0 for everything the provider reads — verified by
  diffing both payloads for the same running instance — including the
  `{"22/tcp": [{"HostIp": …, "HostPort": …}]}` port mapping, so SSH and
  port forwarding are unaffected. Only the collection listing was
  deprecated: GPU search, provisioning, start/stop, and terminate have no
  v1 equivalents and continue to use v0.

### Changed
- Single-pod lookups resolve through a server-side `select_filters` id
  filter instead of scanning the full instance list. Vast.ai exposes no v1
  single-instance endpoint and v1 caps pages at 25 rows, so the filter
  keeps the refresh after each start/stop at one request.
- Pagination sends the cursor as `after_token`, the request-side name for
  the response's `next_token`. Vast.ai ignores unknown query parameters
  rather than rejecting them, so the mismatched name would re-request the
  first page indefinitely; the loop also stops if a cursor ever repeats.

## [0.2.10] - 2026-07-26

### Fixed
- **ComfyUI PyTorch auto-repair now detects GPU-architecture mismatches.**
  The pre-start check compared only CUDA runtime versions (a cu124 torch
  under a 12.8+ driver passes numerically), so a workspace venv restored
  onto a newer GPU generation — e.g. a cu124 build on a Blackwell B200
  (sm_100) — sailed through the check and ComfyUI died at runtime with
  "CUDA error: no kernel image is available for execution on the device".
  The check now executes a real CUDA op, so any unrunnable install —
  missing architecture kernels, torch newer than the driver, wedged
  packages — fails the check and triggers the reinstall path.
- **PyTorch wheel-index selection no longer shells out to `nvidia-smi`.**
  Marketplace hosts sometimes replace `nvidia-smi` with broken wrapper
  scripts (observed on a Vast.ai host whose GSP-workaround stub exec'd a
  nonexistent path), which made CUDA-version detection silently return
  empty and fall back to reinstalling the same wrong build. Detection now
  queries NVML directly via ctypes (`nvmlSystemGetCudaDriverVersion_v2`,
  `nvmlDeviceGetCudaComputeCapability` — the approach used by WheelNext's
  nvidia-variant-provider), falling back to the kernel-provided
  `/proc/driver/nvidia/version` when NVML is unavailable.
- `swm download <path>` now works with the `swm use` active pod. Click
  fills positional arguments greedily, so a lone path landed in
  `[INSTANCE_ID]` and parsing failed with "Missing argument 'REMOTE_PATH'"
  before the active-pod fallback could run. A lone argument now shifts to
  `REMOTE_PATH`, and the pod resolves from the active selection.

### Changed
- PyTorch wheel-index selection is architecture-aware and tracks the
  current PyTorch support matrix: pre-Turing GPUs (< sm_75) are routed to
  the cu126 legacy tier (retained through torch 2.14), and driver CUDA
  ≥ 13.0 now selects cu130 — the old table capped at cu128, which strands
  modern GPUs on torch ≤ 2.11 now that cu128 wheels are discontinued.

## [0.2.9] - 2026-07-18

### Fixed
- `ensure_python`'s self-heal for uv's materialized Python minor-version
  symlinks now repairs *every* stale `cpython-X.Y-*` slot under
  `/workspace/.python`, not just the minor currently being installed. `uv
  python install` reconciles the minor-version symlink for every Python
  version it finds in the install dir on each invocation, so a single
  leftover duplicate from an unrelated/old minor (e.g. a stray 3.12 install
  predating a framework's pin to 3.11) was still enough to fail installing
  3.11 with "Is a directory (os error 21)" and abort framework setup
  entirely — even after the partial fix in 0.2.8.

## [0.2.7] - 2026-07-13

### Added
- `swm.config.overlay(values)` context manager: temporarily overlays flat
  dot-key config values (e.g. `{"runpod.api_key": "..."}`) for the current
  execution context, consulted by `config.get()` before the on-disk TOML.
  This lets programmatic embedders (e.g. a server) inject per-request
  credentials without mutating the user's `~/.config/swm/config.toml`. With no
  active overlay, CLI behavior is byte-for-byte identical.

## [0.2.6] - 2026-06-16

### Fixed
- **Auto-down never fired on workspace-backed pods (cost bug).** The lifecycle
  guard's filesystem-activity signal keyed off the mtime of the autosync watcher
  log (`/workspace/.swm_changes.log`), which autosync rewrites every cycle — so
  pods were reported perpetually "active," the idle timer never elapsed, and
  `auto-down`/`auto-stop` never triggered. The guard now detects *real*
  workspace writes via `find`, excluding swm bookkeeping (`.swm_*`,
  `.swm_guard`), build caches (`.uv-cache`, `.cache`, `__pycache__`), logs,
  `terminfo`, `.git`, and `.nv`.
- `swm setup start` no longer crashes with a traceback when the post-start
  health probe hits a connection reset. `_probe_url` caught `ConnectError`/
  `TimeoutException` but not `httpx.ReadError` ("connection reset by peer" while
  a freshly-opened SSH tunnel settles); it now catches all `httpx.HTTPError`.

### Changed
- Workspace sync now excludes `/.uv-cache/` (uv build/wheel cache) and
  `/terminfo/` (managed-CPython terminal database). The former churned
  constantly and caused delete-reconciliation failures and ~11 GB of wasted
  storage; the latter's dedup-hardlinked files broke hardlink staging.

### Docs
- Rewrote the `swm-gpu-workflow` skill for full command coverage — added the
  entire `swm storage` group, `swm config`, `swm images`, `swm sync auto`,
  `swm setup workspace`/`storage`, `pod terminate` vs `pod down`, and more.

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
