---
name: swm-gpu-workflow
description: >
  Use when the user wants to manage cloud GPU instances, search for GPU
  availability and pricing, provision pods, install AI frameworks (ComfyUI,
  SwarmUI, Axolotl), sync workspaces, or work with the swm CLI tool.
---

# swm — Cloud GPU Workflow Manager

## When To Use This Skill

Activate when the user's task involves:
- Searching for GPU availability or pricing across cloud providers
- Provisioning, starting, stopping, or terminating GPU pods
- Installing AI frameworks on remote GPU instances
- Syncing workspaces between cloud storage and pods
- Transferring files to/from GPU pods
- Configuring swm providers or storage
- Extending swm with new providers or storage backends

## Core Workflow

The standard swm workflow is linear:

```
swm gpus           →  Find available GPUs and compare prices
swm pod create     →  Provision a pod
swm setup install  →  Install a framework (comfyui, swarmui, axolotl, llm-studio)
swm sync pull      →  Pull workspace from storage to pod
  ... work ...
swm pod down       →  Push workspace back and terminate
```

## Key Commands

### GPU Search (always start here)

```bash
swm gpus                          # all GPUs, all providers
swm gpus -g h200                  # filter by GPU name (free text)
swm gpus -g h200 -c 4            # 4×H200 configs
swm gpus -g h200 --secure        # secure cloud only
swm gpus --max-price 4           # under $4/hr on-demand
swm gpus -p vastai               # single provider
swm gpus -n 50                   # show 50 results (default: 20)
swm gpus --all                   # show everything
```

### Pod Management

```bash
swm pod create -p runpod -g h200 -n my-session
swm pod create -p vastai -g h200 -n train --gpu-count 4 --volume 200 --cloud-type SECURE
swm pod list
swm pod status runpod:<id>
swm pod stop runpod:<id>
swm pod start runpod:<id>
swm pod down runpod:<id>         # sync + terminate
```

### Framework Installation

```bash
swm setup list                    # available frameworks
swm setup install comfyui runpod:<id>
swm setup install swarmui vastai:<id>
swm setup start comfyui runpod:<id>
swm setup stop comfyui runpod:<id>
```

### Workspace Sync

```bash
swm sync pull runpod:<id>         # storage → pod
swm sync push runpod:<id>         # pod → storage
swm sync pull runpod:<id> --force # kill stale transfers first
```

### Remote Access

```bash
swm ssh runpod:<id>               # interactive shell
swm run runpod:<id> 'nvidia-smi'  # run a command
swm upload runpod:<id> ./model.safetensors models/
swm download runpod:<id> output/video.mp4 -d ~/Downloads
```

## Instance ID Format

Instances are referenced as `provider:id`:
- `runpod:abc123`
- `vastai:34182944`
- `lambda:inst-xyz`

A bare ID (no provider prefix) auto-resolves by querying all configured providers.

## Providers

| Provider | Slug | API | Live data |
|----------|------|-----|-----------|
| RunPod | `runpod` | GraphQL | prices, stock |
| Vast.ai | `vastai` | REST | prices, stock |
| Lambda Labs | `lambda` | REST | prices, stock |
| AWS | `aws` | boto3 | static only |
| GCP | `gcp` | gcloud CLI | static only |
| CoreWeave | `coreweave` | Kubernetes | static only |

## Storage

All storage uses S3-compatible APIs. Configured via `swm config set`:
- **B2**: `b2.key_id`, `b2.app_key`, `b2.bucket`
- **GCS**: `gcs.hmac_access`, `gcs.hmac_secret`, `gcp.bucket`
- **S3**: `s3.access_key`, `s3.secret_key`, `s3.bucket`

Default storage: `swm config set storage.default b2:<bucket-name>`

## Important Behaviors

- `swm gpus` results are paginated (20 rows default). Use `--all` or `-n N` for more.
- `swm pod create` injects the SSH public key and waits for direct SSH to be reachable before bootstrapping.
- Storage credentials are never written to the pod. They're passed as env vars per s5cmd invocation.
- Workspace sync is non-destructive: pull uses `--no-clobber`, push uses `--size-only`.
- The `--cloud-type SECURE` flag (default for RunPod) restricts to SOC 2 / HIPAA certified data centers.
