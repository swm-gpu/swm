# swm

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)

**One CLI to rule all GPU clouds.**

Search pricing across 10 providers, spin up a GPU in seconds, sync your workspace automatically, and track every dollar — without locking into any single cloud.

```
$ swm gpus -g h200 --max-price 4

  Live GPU Availability
┏━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━┓
┃ Provider ┃ GPU              ┃ VRAM   ┃ $/hr     ┃ Stock   ┃
┡━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━┩
│ vastai   │ NVIDIA H200      │ 141 GB │ $2.89/hr │ 12 avl  │
│ runpod   │ NVIDIA H200      │ 141 GB │ $3.49/hr │ High    │
│ lambda   │ NVIDIA H200      │ 141 GB │ $3.99/hr │ 4 avl   │
│ vultr    │ NVIDIA H200      │ 141 GB │ $3.88/hr │ 8 avl   │
└──────────┴──────────────────┴────────┴──────────┴─────────┘
```

## Why swm?

**You shouldn't need 10 browser tabs to find a GPU.** Cloud GPU pricing changes by the hour. Availability disappears in minutes. And every provider has a different dashboard, API, and workflow.

swm gives you:

- **One search** across RunPod, Vast.ai, Lambda Labs, Vultr, TensorDock, FluidStack, AWS, GCP, Azure, and CoreWeave
- **One command** to provision, bootstrap, and connect via SSH
- **One workspace** that follows you across providers — stored in Backblaze B2, S3, or GCS, pulled on create, pushed on shutdown
- **One bill view** showing card charges, GPU usage, and cost tracking across every provider

## Supported Providers

| Provider | GPU Search | Provision | Stop/Resume | Billing |
|----------|-----------|-----------|-------------|---------|
| RunPod | Live | Yes | Yes | Full (card + usage) |
| Vast.ai | Live | Yes | Yes | Full (card + usage) |
| Lambda Labs | Live | Yes | — | — |
| Vultr | Live | Yes | Yes | — |
| TensorDock | Live | Yes | Yes | — |
| FluidStack | Live | Yes | Yes | — |
| AWS (EC2) | Live | Yes | Yes | — |
| GCP (Compute) | Live | Yes | Yes | — |
| Azure | Live | Yes | Yes | — |
| CoreWeave | Live | Yes | Yes | — |

## Install

```bash
# macOS (Homebrew)
brew tap swm-dev/swm && brew install swm

# Python (3.11+)
pipx install swm

# From source
pip install .
```

## Quick Start

```bash
# 1. Add your API key (takes 30 seconds)
swm config set runpod.api_key <your-key>

# 2. Set up workspace storage (Backblaze B2, S3, or GCS)
swm config set b2.key_id <key-id>
swm config set b2.app_key <app-key>
swm config set b2.bucket my-workspace
swm config set storage.default b2:my-workspace

# 3. Find a GPU
swm gpus -g h200

# 4. Create a pod (provisions, injects SSH key, pulls your workspace)
swm pod create -p runpod -g h200 -n my-session

# 5. Install a framework
swm setup install comfyui runpod:<id>

# 6. Work — SSH in, run jobs, generate images
swm ssh runpod:<id>

# 7. Done — pushes workspace to storage, terminates the pod
swm pod down runpod:<id>
```

Your workspace (models, outputs, configs) persists in cloud storage. Next time you spin up a pod — on any provider — it's all there.

## Core Commands

### Search GPUs

```bash
swm gpus                            # all GPUs, all providers
swm gpus -g h200                    # filter by GPU name
swm gpus -g h200 -c 4              # 4-GPU configs only
swm gpus --max-price 4             # under $4/hr
swm gpus --secure                  # secure/certified clouds only
```

### Manage Pods

```bash
swm pod create -p runpod -g h200 -n train-run   # provision
swm pod list                                      # list all instances
swm pod stop runpod:<id>                          # pause billing
swm pod start runpod:<id>                         # resume
swm pod down runpod:<id>                          # sync + terminate
```

### Sync Workspace

```bash
swm sync pull runpod:<id>           # storage -> pod
swm sync push runpod:<id>           # pod -> storage
swm sync watch runpod:<id>          # auto-push on file changes
```

### Install Frameworks

```bash
swm setup list                      # see available frameworks
swm setup install comfyui runpod:<id>
swm setup install swarmui runpod:<id>
swm setup install axolotl runpod:<id>
swm setup start comfyui runpod:<id>
```

Supported frameworks: **ComfyUI**, **SwarmUI**, **Axolotl**, **H2O LLM Studio**

### Track Costs

```bash
swm costs live                      # running cost right now
swm costs summary                   # spending breakdown (30 days)
swm costs reconcile                 # card charges + usage from provider APIs
swm costs budget set 100            # $100/month budget alert
```

### Remote Access

```bash
swm ssh runpod:<id>                 # interactive shell
swm run runpod:<id> 'nvidia-smi'   # run a command
swm upload runpod:<id> ./model.safetensors
swm download runpod:<id> output/video.mp4
```

## How It Works

swm operates entirely over **SSH**. No agents, no custom Docker images, no vendor lock-in.

```
┌──────────┐       SSH        ┌─────────────┐       S3 API      ┌───────────┐
│ Your Mac │ ───────────────> │  GPU Pod    │ ────────────────> │ B2 / S3   │
│   swm    │  exec, scp      │  (any       │  s5cmd sync       │ / GCS     │
│          │ <─────────────── │   provider) │ <──────────────── │(workspace)│
└──────────┘                  └─────────────┘                   └───────────┘
```

1. **Provision** — swm calls the provider API, injects your SSH public key
2. **Connect** — Direct SSH to the pod (no relay, no proxy)
3. **Bootstrap** — Installs s5cmd, pulls your workspace from cloud storage
4. **Work** — Run frameworks, train models, generate images
5. **Shutdown** — Pushes workspace back to storage, terminates the pod

Credentials are never stored on the pod. Storage keys are passed as transient environment variables per command.

## Storage

swm syncs your `/workspace` directory to S3-compatible cloud storage. Supported backends:

| Backend | Setup |
|---------|-------|
| **Backblaze B2** | `swm config set b2.key_id` / `b2.app_key` / `b2.bucket` |
| **Amazon S3** | `swm config set s3.access_key` / `s3.secret_key` / `s3.bucket` |
| **Google Cloud Storage** | `swm config set gcs.hmac_access` / `gcs.hmac_secret` |

Syncs are **non-destructive** — pull skips existing files, push only uploads new/changed files, nothing is ever deleted.

See [Storage Setup](docs/storage.md) for detailed configuration.

## Cost Tracking

swm tracks every GPU session locally and reconciles against provider billing APIs.

```bash
$ swm costs reconcile

runpod — last 30 days
  Balance:        $46.78
  Spend rate:     $3.24/hr

  Card charges
  ┏━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━━┳━━━━━━━━┓
  ┃ Date       ┃ Method ┃ Description       ┃ Amount ┃
  ┡━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━━╇━━━━━━━━┩
  │ 2026-04-09 │ Stripe │ Auto-reload       │ $50.00 │
  │ 2026-04-07 │ Stripe │ Auto-reload       │ $50.00 │
  └────────────┴────────┴───────────────────┴────────┘

  Usage by GPU
  ┏━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━┓
  ┃ Resource                ┃ Hours ┃   Cost ┃
  ┡━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━┩
  │ NVIDIA H200             │ 11.3h │ $41.92 │
  │ NVIDIA B200             │  9.1h │ $48.98 │
  └─────────────────────────┴───────┴────────┘
```

Set budget alerts to get warned before overspending:

```bash
swm costs budget set 50                         # $50/month global
swm costs budget set 100 --scope provider:runpod # $100/month for RunPod
swm costs budget set 10 --period daily           # $10/day
```

## Security

- **SSH key authentication only** — no passwords stored anywhere
- **No credentials on pods** — storage keys are passed transiently over SSH, never written to disk
- **Non-destructive syncs** — files are never deleted from your storage bucket
- **Secure cloud default** — `swm pod create` defaults to SOC 2 / HIPAA certified data centers
- **S3 over HTTPS** — all storage transfers use TLS

## Documentation

Full docs at [swmgpu.com](https://swmgpu.com/overview/).

| Page | Description |
|------|-------------|
| [Getting Started (CLI)](https://swmgpu.com/getting-started/for-cli-users/) | Install and create your first pod in 5 minutes |
| [Getting Started (Agent)](https://swmgpu.com/getting-started/for-agent-users/) | Let your AI agent manage GPUs for you |
| [Command Reference](https://swmgpu.com/commands/gpus/) | Full reference for every swm command |
| [Core Concepts](https://swmgpu.com/concepts/providers/) | Providers, workspaces, frameworks, lifecycle guard |
| [Configuration](https://swmgpu.com/getting-started/configuration/) | All config keys for providers and storage |

## Requirements

- macOS or Linux
- Python 3.11+ (if not using Homebrew binary)
- SSH client (`ssh`, `scp`)
- An account with at least one GPU provider

## License

Licensed under the [Apache License, Version 2.0](LICENSE).

```
Copyright 2025 swm contributors
```
