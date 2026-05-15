<p align="center">
  <img src="https://raw.githubusercontent.com/swm-gpu/swm/main/.github/logo.svg" alt="swm" width="400" />
</p>

<p align="center">
  <a href="https://opensource.org/licenses/Apache-2.0"><img src="https://img.shields.io/badge/License-Apache_2.0-blue.svg" alt="License" /></a>
  <a href="https://pypi.org/project/swm-gpu/"><img src="https://img.shields.io/pypi/v/swm-gpu.svg" alt="PyPI" /></a>
  <a href="https://pypi.org/project/swm-gpu/"><img src="https://img.shields.io/pypi/pyversions/swm-gpu.svg" alt="Python" /></a>
</p>

<p align="center">
  <b>One CLI to rule all GPU clouds.</b><br/>
  Search pricing across 10 providers, spin up a GPU in seconds, sync your workspace, and track every dollar.
</p>

---

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

## Install

```bash
# macOS (Homebrew)
brew tap swm-gpu/swm && brew install swm

# Python (3.11+)
pipx install swm-gpu

# From source
git clone https://github.com/swm-gpu/swm.git && cd swm && pip install -e .
```

## Quick Start

```bash
# 1. Add your API key
swm config set runpod.api_key <your-key>

# 2. Find a GPU (the table tells you each GPU's minimum CUDA)
swm gpus -g h200

# 3. Create a pod (auto-picks a CUDA-compatible image; auto-saves to storage)
swm pod create -p runpod -g h200 -n my-session --cuda 12.8

# 4. Install a framework
swm setup install vllm runpod:<id>

# 5. Done — pushes workspace to storage and terminates
swm pod down runpod:<id>
```

## Or just ask your agent

Don't want to learn the CLI? Install the [SKILL.md](.agents/skills/swm-gpu-workflow/SKILL.md) and your AI agent manages GPUs for you:

```bash
# Universal (works with Cursor, Copilot, Windsurf, Amp, Devin)
mkdir -p .agents/skills/swm-gpu-workflow
curl -sL https://raw.githubusercontent.com/swm-gpu/swm/main/.agents/skills/swm-gpu-workflow/SKILL.md \
  -o .agents/skills/swm-gpu-workflow/SKILL.md
```

Works with **Cursor**, **Claude Code**, **Codex**, **Copilot**, **Windsurf**, **Amp**, **Devin**, and any agent that can run shell commands.

## Supported Providers

| Provider | GPU Search | Provision | Stop/Resume | Billing API |
|----------|-----------|-----------|-------------|-------------|
| RunPod | Live | Yes | Yes | Full |
| Vast.ai | Live | Yes | Yes | Full |
| Lambda Labs | Live | Yes | — | — |
| Vultr | Live | Yes | Yes | — |
| TensorDock | Live | Yes | Yes | — |
| FluidStack | Live | Yes | Yes | — |
| AWS (EC2) | Live | Yes | Yes | — |
| GCP (Compute) | Live | Yes | Yes | — |
| Azure | Live | Yes | Yes | — |
| CoreWeave | Live | Yes | Yes | — |

## Key Features

### GPU Search & Provisioning

```bash
swm gpus                            # all GPUs, all providers
swm gpus -g h200 -c 4              # 4×H200 configs
swm gpus --max-price 4 --secure    # under $4/hr, certified clouds
swm images list -p runpod --cuda 12.8  # see compatible Docker images
swm pod create -p runpod -g h200 -n train --cuda 12.8
swm pod down runpod:<id>            # sync + terminate
```

`swm gpus` reports each GPU's minimum CUDA toolkit. Pass `--cuda <major.minor>` to `swm pod create` to auto-pick the newest provider image that satisfies it.

### Workspace Sync

Your `/workspace` directory follows you across clouds via S3-compatible storage (Backblaze B2, Amazon S3, Google GCS).

```bash
swm sync pull runpod:<id>           # storage → pod
swm sync push runpod:<id>           # pod → storage (incremental)
swm sync push runpod:<id> --delete  # also remove files deleted locally
swm sync watch runpod:<id>          # filesystem change watcher
swm sync auto runpod:<id>           # background daemon: push every 60s
```

Three-tier smart sync: inotify watcher tracks changes, incremental push uploads only what changed, tar mode packs 600k small files into one S3 object.

**Continuous auto-sync.** `swm pod create` starts an auto-sync daemon by default — it tails the watcher log and pushes every 60s with no manual intervention. Adopt an existing pod with `swm setup workspace <pod>` if you created it with `--no-storage` or the bootstrap was interrupted.

### Frameworks

```bash
swm setup install vllm runpod:<id>       # vLLM inference server
swm setup install open-webui runpod:<id> # Open WebUI chat interface
swm setup install comfyui runpod:<id>    # ComfyUI image generation
swm setup install axolotl runpod:<id>    # Axolotl fine-tuning
swm setup install ollama runpod:<id>     # Ollama model runner
swm setup install swarmui runpod:<id>    # SwarmUI
swm setup install llm-studio runpod:<id> # H2O LLM Studio
```

Auto-detects GPU count for tensor parallelism, opens SSH tunnels for unexposed ports, probes health endpoints.

### Lifecycle Guard

Monitors SSH sessions, GPU utilization, filesystem writes, and active processes. If nothing's happening, it saves your workspace and terminates the pod.

```bash
swm pod create -p runpod -g h200 -n train \
  --lifecycle auto-down --idle-timeout 30   # bake the policy into create
swm guard set runpod:<id> --mode auto-down --idle-timeout 30
swm guard list
```

No more $96 overnight H100 bills.

### Cost Tracking

```bash
swm costs live                      # running cost right now
swm costs summary                   # spending breakdown
swm costs reconcile                 # verify against provider billing APIs
swm costs budget set 100            # $100/month alert
```

### Model Management

```bash
swm models search qwen3             # search HuggingFace Hub
swm models pull runpod:<id> Qwen/Qwen3-235B
swm models set runpod:<id> Qwen/Qwen3-235B  # hot-swap vLLM model
```

## How It Works

Everything happens over **SSH**. No agents on the pod. No custom images. No webhooks.

```
┌──────────┐       SSH        ┌─────────────┐       S3 API      ┌───────────┐
│ Your Mac │ ───────────────> │  GPU Pod    │ ────────────────> │ B2 / S3   │
│   swm    │  exec, scp      │  (any       │  s5cmd sync       │ / GCS     │
│          │ <─────────────── │   provider) │ <──────────────── │(workspace)│
└──────────┘                  └─────────────┘                   └───────────┘
```

Credentials are never stored on the pod. Storage keys are passed as transient environment variables per command.

## Security

- **SSH key authentication only** — no passwords stored anywhere
- **No credentials on pods** — storage keys passed transiently, never written to disk
- **Non-destructive by default** — `sync push`, `sync pull`, and `pod down` never remove files from your storage bucket. Deletions are opt-in (`sync push --delete`) and the auto-sync daemon refuses to start unless a prior pull/push has confirmed pod ↔ bucket are in sync
- **Secure cloud default** — `swm pod create` defaults to SOC 2 / HIPAA certified data centers

## Documentation

Full docs at **[swmgpu.com](https://swmgpu.com/overview/)**.

| Page | Description |
|------|-------------|
| [Getting Started (CLI)](https://swmgpu.com/getting-started/for-cli-users/) | Install and create your first pod in 5 minutes |
| [Getting Started (Agent)](https://swmgpu.com/getting-started/for-agent-users/) | Let your AI agent manage GPUs for you |
| [Configuration](https://swmgpu.com/getting-started/configuration/) | All config keys for providers and storage |
| [Command Reference](https://swmgpu.com/commands/gpus/) | Full reference for every swm command |
| [Core Concepts](https://swmgpu.com/concepts/providers/) | Providers, workspaces, frameworks, lifecycle guard |

## Requirements

- macOS or Linux
- Python 3.11+ (if not using Homebrew binary)
- SSH client (`ssh`, `scp`)
- An account with at least one GPU provider

## Contributing

Bug reports, feature requests, and pull requests welcome. See
[CONTRIBUTING.md](./CONTRIBUTING.md) for scope, code style, and the PR
workflow. The community is governed by our
[Code of Conduct](./CODE_OF_CONDUCT.md).

Open-ended questions and design discussions belong in
[GitHub Discussions](https://github.com/swm-gpu/swm/discussions).
Security reports go through
[private vulnerability reporting](https://github.com/swm-gpu/swm/security/advisories/new) —
see [SECURITY.md](./SECURITY.md).

## License

Licensed under the [Apache License, Version 2.0](LICENSE).
