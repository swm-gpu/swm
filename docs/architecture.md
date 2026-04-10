# How swm Works

## SSH as the Control Plane

swm uses **SSH** as the single communication channel to GPU pods. No custom agents, no modified Docker images, no proprietary protocols.

```
┌──────────┐       SSH (direct TCP)       ┌─────────────┐
│ Your Mac │ ──────────────────────────>  │  GPU Pod    │
│   swm    │  commands, file transfers    │  (sshd)     │
│          │ <──────────────────────────  │             │
└────┬─────┘                              └──────┬──────┘
     │                                           │
     │  Provider API                             │  s5cmd (S3 API)
     │  (create, stop, terminate)                │  (workspace sync)
     v                                           v
┌──────────────────┐                     ┌───────────────┐
│  RunPod, Vast.ai │                     │  B2 / S3 / GCS│
│  Lambda, Vultr   │                     │  (workspace)  │
│  AWS, GCP, Azure │                     └───────────────┘
│  TensorDock, etc.│
└──────────────────┘
```

**Why SSH instead of an agent?**

- No credentials stored on the pod — storage keys are passed as transient environment variables per command
- No custom Docker images — the provider's stock image runs unmodified
- Any pod with `sshd` is compatible — adding new providers is straightforward
- Full interactive visibility — bootstrap output streams to your terminal in real time

## Pod Lifecycle

### 1. Provisioning

When you run `swm pod create`, swm:

1. Calls the provider's API to create an instance
2. Injects your SSH **public key** via the provider's environment variable mechanism
3. The provider's Docker image starts `sshd` and writes the key to `~/.ssh/authorized_keys`
4. swm waits for the SSH port to become reachable (not just the API status)

### 2. Bootstrap

Once SSH is up, swm runs a series of commands over SSH:

1. **Install s5cmd** — a high-performance S3 client for parallel file transfers
2. **Configure storage** — verifies connectivity to your B2/S3/GCS bucket
3. **Pull workspace** — copies your workspace from cloud storage to `/workspace` on the pod
4. **Install inotify** — sets up a filesystem watcher for automatic push-on-change (optional)

### 3. Working

The pod is now ready. You can:

- SSH in interactively with `swm ssh`
- Install frameworks with `swm setup install`
- Run commands with `swm run`
- Transfer files with `swm upload` / `swm download`

### 4. Shutdown

`swm pod down` performs a clean shutdown:

1. Pushes `/workspace` to cloud storage (only new/changed files)
2. Terminates the instance via the provider API
3. Cleans up local metadata

Your workspace persists in cloud storage. Spin up a new pod on any provider and your files are restored.

## Workspace Sync

Workspaces are named directories in a cloud storage bucket (e.g., `b2:my-bucket/workspace`).

**Pull** (storage to pod): Uses `s5cmd cp --no-clobber` — downloads files that don't already exist on the pod. Existing files are never overwritten.

**Push** (pod to storage): Uses a three-tier strategy:
1. **Inotify watcher** — if running, instantly pushes files as they change
2. **Timestamp-based delta** — finds files modified since the last push using `find -newer`
3. **Full scan** — falls back to `s5cmd cp --if-size-differ` for a complete comparison

Both directions are non-destructive. Files are never deleted from cloud storage.

## Provider Abstraction

All 10 GPU providers implement a common interface. swm normalizes each provider's data into shared types:

- **GpuInfo** — GPU availability, pricing, VRAM, stock level
- **Instance** — Running pod with SSH connection details
- **CreateConfig** — Parameters for provisioning (GPU type, count, volume size, etc.)

This means every swm command works identically regardless of provider. `swm gpus` queries all providers in parallel. `swm pod create -p vastai` and `swm pod create -p runpod` follow the same flow.

## Storage Abstraction

All storage backends (Backblaze B2, Amazon S3, Google Cloud Storage) use the S3-compatible API via boto3. One code path handles all three — no provider-specific CLIs needed.

Storage is used for:
- **Workspace persistence** — `/workspace` synced via s5cmd on the pod
- **Direct bucket operations** — `swm storage ls`, `swm storage upload`, etc.

## Security Model

- **SSH key auth only** — no passwords, no tokens stored on pods
- **Transient credentials** — storage keys are passed as environment variables per s5cmd invocation, never written to the pod's filesystem
- **Secure cloud default** — `swm pod create` defaults to `--cloud-type SECURE` (SOC 2 / HIPAA certified data centers on supported providers)
- **TLS everywhere** — all S3 API calls use HTTPS endpoints
