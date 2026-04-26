---
name: swm-gpu-workflow
description: >
  Use this skill whenever the user wants to search for cloud GPUs, provision
  GPU pods, install AI frameworks (vLLM, Open WebUI, Ollama, ComfyUI, SwarmUI,
  Axolotl, H2O LLM Studio), sync workspaces, manage lifecycle guards, track
  costs, or work with the swm CLI across any of 10 GPU cloud providers.
  Do NOT use for general Docker, Kubernetes, or non-GPU cloud tasks.
license: Apache-2.0
compatibility: "macOS or Linux, Python 3.11+, SSH client"
metadata:
  author: swm-gpu
  version: "0.1.0"
  docs-url: https://swmgpu.com
  repository: https://github.com/swm-gpu/swm
---

# swm — Cloud GPU Workflow Manager

One CLI to search, provision, and manage cloud GPUs across 10 providers.

## Prerequisites

Before using any swm command, ensure:
1. **swm is installed**: `swm --version` should print a version number.
   - Install via `pipx install swm-gpu` or `brew tap swm-gpu/swm && brew install swm`
2. **SSH client available**: `ssh` and `scp` must be on PATH.
3. **At least one provider API key configured**: `swm config list` shows configured keys.
   - Example: `swm config set runpod.api_key <key>`
4. **Storage configured** (optional, for workspace sync): `swm config set b2.key_id <key>` etc.

If swm is not installed, install it first:
```bash
pipx install swm-gpu
```

## When To Use This Skill

Activate when the user's task involves:
- Searching for GPU availability or pricing across cloud providers
- Provisioning, starting, stopping, or terminating GPU pods
- Installing AI frameworks on remote GPU instances
- Syncing workspaces between cloud storage and pods
- Transferring files to/from GPU pods
- Monitoring GPU costs or setting budgets
- Managing lifecycle guards (auto-stop, auto-terminate on idle)
- Searching or deploying HuggingFace models
- Configuring swm providers or storage

## Core Workflow (6 Phases)

Follow this sequence for every GPU task:

### Phase 0: State Check
```bash
swm pod list
```
Check for existing pods before creating new ones.

### Phase 1: Clarify
Ask the user:
- What VRAM is needed? (determines GPU class)
- Provider preference? (or let swm find cheapest)
- Lifecycle policy? (`auto-down`, `auto-stop`, `remind`, `manual`)
- Idle timeout? (default: 30m)
- Persist workspace to S3? (yes/no)

### Phase 2: Pick GPU
```bash
swm gpus -g <gpu-name> --sort price
swm gpus -g h200 -c 4 --max-price 4 --secure
```
Select the cheapest in-stock option that meets VRAM requirements.

### Phase 3: Provision
```bash
swm pod create -p <provider> -g <gpu> -n <name> -y
```
Always pass `-y` to skip confirmation prompts in agent context.

### Phase 4: Install
```bash
swm setup install <framework> <pod-id>
```
Or for custom tools:
```bash
swm run <pod-id> "apt-get install -y <package>"
swm run <pod-id> "pip install <package>"
```

### Phase 5: Verify
```bash
swm run <pod-id> "nvidia-smi"
swm run <pod-id> "curl -s localhost:8000/v1/models"
swm run <pod-id> "df -h /workspace"
```
Check GPU memory, HTTP endpoints, and disk space before handing off.

### Phase 6: Hand Off
Report to the user:
- Pod ID and SSH command
- Service URL (if applicable)
- Lifecycle policy in effect
- How to terminate: `swm pod down <pod-id>`

## Key Commands

### GPU Search (always start here)

```bash
swm gpus                          # all GPUs, all providers
swm gpus -g h200                  # filter by GPU name (free text)
swm gpus -g h200 -c 4            # 4×H200 configs
swm gpus -g h200 --secure        # secure cloud only (SOC 2 / HIPAA)
swm gpus --max-price 4           # under $4/hr on-demand
swm gpus -p vastai               # single provider
swm gpus -n 50                   # show 50 results (default: 20)
swm gpus --all                   # show everything
swm gpus --sort price            # sort by price ascending
```

### Pod Management

```bash
swm pod create -p runpod -g h200 -n my-session -y
swm pod create -p vastai -g h200 -n train --gpu-count 4 --volume 200 --cloud-type SECURE -y
swm pod list                     # all active pods
swm pod status <pod-id>
swm pod stop <pod-id>            # pause (keeps disk)
swm pod start <pod-id>           # resume paused pod
swm pod down <pod-id>            # sync workspace + terminate
```

### Framework Installation

7 built-in frameworks:

```bash
swm setup list                               # show available frameworks
swm setup install vllm <pod-id>              # vLLM inference server
swm setup install open-webui <pod-id>        # Open WebUI chat interface
swm setup install ollama <pod-id>            # Ollama model runner
swm setup install comfyui <pod-id>           # ComfyUI image generation
swm setup install swarmui <pod-id>           # SwarmUI
swm setup install axolotl <pod-id>           # Axolotl fine-tuning
swm setup install llm-studio <pod-id>        # H2O LLM Studio
swm setup start <framework> <pod-id>         # start framework
swm setup stop <framework> <pod-id>          # stop framework
```

Auto-detects GPU count for tensor parallelism. Opens SSH tunnels for unexposed ports. Probes health endpoints before reporting ready.

### Workspace Sync

```bash
swm sync pull <pod-id>           # storage → pod
swm sync push <pod-id>           # pod → storage
swm sync pull <pod-id> --force   # kill stale transfers first
swm sync watch <pod-id>          # auto-push on file changes (inotify)
```

Three-tier smart sync: inotify watcher → incremental s5cmd → tar mode for 600k+ small files.

### Lifecycle Guard

```bash
swm guard enable <pod-id> --policy auto-down --idle 30m
swm guard disable <pod-id>
swm guard list                   # status of all guards
```

Monitors SSH sessions, GPU utilization, filesystem writes, and active processes. If idle beyond threshold, saves workspace and terminates.

Policies: `auto-down` (sync + terminate), `auto-stop` (pause), `remind` (notify only), `manual` (no action).

### Cost Tracking

```bash
swm costs live                   # running cost right now
swm costs summary                # spending breakdown by provider/pod
swm costs reconcile              # verify against provider billing APIs
swm costs budget set 100         # $100/month alert
```

### Model Management

```bash
swm models search <query>        # search HuggingFace Hub
swm models pull <pod-id> <model> # download model to pod
swm models set <pod-id> <model>  # hot-swap vLLM model
swm models set <pod-id> <model> --restart  # restart vLLM with new model
```

### Remote Access

```bash
swm ssh <pod-id>                 # interactive shell
swm run <pod-id> '<command>'     # run a command remotely
swm upload <pod-id> ./local/file remote/path
swm download <pod-id> remote/file -d ~/Downloads
```

## Instance ID Format

Pods are referenced as `provider:id`:
- `runpod:abc123`
- `vastai:34182944`
- `lambda:inst-xyz`
- `vultr:vm-abc`

A bare ID (no provider prefix) auto-resolves by querying all configured providers.

## Providers

| Provider | Slug | API | Live pricing |
|----------|------|-----|-------------|
| RunPod | `runpod` | GraphQL | Yes |
| Vast.ai | `vastai` | REST | Yes |
| Lambda Labs | `lambda` | REST | Yes |
| Vultr | `vultr` | REST | Yes |
| TensorDock | `tensordock` | REST | Yes |
| FluidStack | `fluidstack` | REST | Yes |
| AWS (EC2) | `aws` | boto3 | Yes |
| GCP (Compute) | `gcp` | gcloud CLI | Yes |
| Azure | `azure` | az CLI | Yes |
| CoreWeave | `coreweave` | Kubernetes | Yes |

## Storage

All storage uses S3-compatible APIs via s5cmd. Configured with `swm config set`:

| Backend | Config keys |
|---------|------------|
| Backblaze B2 | `b2.key_id`, `b2.app_key`, `b2.bucket` |
| Amazon S3 | `s3.access_key`, `s3.secret_key`, `s3.bucket` |
| Google GCS | `gcs.hmac_access`, `gcs.hmac_secret`, `gcp.bucket` |

Default storage: `swm config set storage.default b2:<bucket-name>`

## Anti-Patterns to Avoid

- **Never omit `-y`** on `swm pod create` — it hangs waiting for confirmation in agent context
- **Never install to container root** — always use `/workspace` (data persists across stop/start)
- **Never use `pip install -e .`** for projects with `[tool.uv.sources]` — use `pip install .` instead
- **Never mix venvs** across frameworks with conflicting torch versions
- **Never hand off** before all health checks pass
- **Never skip Phase 0** — always check for existing pods first

## Important Behaviors

- `swm gpus` results are paginated (20 rows default). Use `--all` or `-n N` for more.
- `swm pod create` injects SSH public key and waits for direct SSH before bootstrapping.
- Storage credentials are never written to the pod — passed as transient env vars per s5cmd call.
- Workspace sync is non-destructive: pull uses `--no-clobber`, push uses `--size-only`.
- `--cloud-type SECURE` (default for RunPod) restricts to SOC 2 / HIPAA certified data centers.
- Guard monitors 4 signals: SSH sessions (`who`), GPU utilization, filesystem writes, active processes.
