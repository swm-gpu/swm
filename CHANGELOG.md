# Changelog

All notable changes to swm are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
