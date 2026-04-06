# swm — Cloud GPU Workflow Manager

A CLI tool for managing cloud GPU instances, storage, and AI workloads (ComfyUI, SwarmUI, video generation) across multiple providers. `swm` treats remote GPU pods as disposable compute — your workspace lives in cloud storage, and `swm` orchestrates everything over standard SSH.

## Workflow

```
  swm gpus           →  Search live availability & pricing
  swm pod create     →  Provision a GPU instance
  swm setup install  →  Install frameworks (ComfyUI, SwarmUI, ...)
  swm sync pull/push →  Sync workspace with cloud storage
  swm costs live     →  Show running cost of active pods
  swm costs summary  →  Spending breakdown by provider/GPU
  swm pod down       →  Push workspace and terminate
```

## Quick Start

```bash
pip install -e "."

# Enable tab completion (bash)
echo 'eval "$(_SWM_COMPLETE=bash_source swm)"' >> ~/.bash_profile
source ~/.bash_profile

# For zsh (common on macOS):
# echo 'eval "$(_SWM_COMPLETE=zsh_source swm)"' >> ~/.zshrc && source ~/.zshrc

# Configure a GPU provider
swm config set runpod.api_key <your-key>

# Configure storage
swm config set b2.key_id <key-id>
swm config set b2.app_key <app-key>
swm config set b2.bucket <bucket-name>
swm config set storage.default b2:<bucket-name>
```

## Everyday Commands

```bash
# 1. Search — find the best GPU deal right now
swm gpus                                          # all GPUs, all providers
swm gpus -g h200                                  # filter to H200s
swm gpus -g h200 -c 4 --secure                   # 4×H200, secure cloud only
swm gpus --max-price 4                            # under $4/hr

# 2. Create — provision a pod
swm pod create -p runpod -g h200 -n my-session
swm pod create -p vastai -g h200 -n train --gpu-count 4 --volume 500

# 3. Install & manage frameworks
swm setup install comfyui runpod:<id>
swm setup start comfyui runpod:<id>

# 4. Workspace sync
swm sync pull runpod:<id>
swm sync push runpod:<id>

# 5. Remote access
swm ssh runpod:<id>
swm run runpod:<id> 'nvidia-smi'

# 6. Cost tracking
swm costs live                                    # running cost of active pods
swm costs summary                                 # spending breakdown (last 30 days)
swm costs budget set 50                           # $50/month global budget
swm costs reconcile                               # compare with provider billing

# 7. Shut down
swm pod down runpod:<id>
```

## Project Structure

```
src/swm/
├── cli.py               # Click CLI — all user-facing commands
├── config.py            # TOML config (~/.config/swm/config.toml)
├── bootstrap.py         # Remote setup: s5cmd, frameworks, workspace sync
├── pricing/             # Static GPU specs + cost estimation
├── costs/              # Cost tracking (SQLite sessions, budgets, provider reconciliation)
├── providers/           # GPU provider implementations (RunPod, Vast.ai, Lambda, AWS, GCP, CoreWeave)
├── frameworks/          # Framework registry (ComfyUI, SwarmUI, Axolotl, LLM Studio)
├── remote/              # SSH session, SCP, key management
└── storage/             # S3-compatible storage (B2, GCS, S3)
```

## Docs

| Page | Contents |
|------|----------|
| [Architecture](docs/architecture.md) | SSH control plane, provider/storage abstraction, workspace lifecycle, design patterns |
| [Storage Setup](docs/storage.md) | Backblaze B2, Google Cloud Storage, Amazon S3 credential setup |
| [CLI Reference](docs/cli-reference.md) | Full command reference for all `swm` commands |
| [Configuration](docs/configuration.md) | All config keys, dependencies, system tools |
| [Extending](docs/extending.md) | How to add new GPU providers and storage backends |

## Security

- **SSH key authentication only** — no passwords stored anywhere
- **No credentials on pods** — storage keys are passed over SSH, never in pod env vars
- **Non-destructive syncs** — `s5cmd cp --no-clobber` (pull) and `s5cmd sync --size-only` (push)
- **Secure cloud default** — `swm pod create` defaults to `--cloud-type SECURE`
- **S3 over HTTPS** — all storage API calls use TLS endpoints
