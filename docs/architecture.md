# Architecture

## Core Design: SSH as the Single Control Plane

`swm` is the sole orchestrator. Pods are dumb GPU machines. Every operation — bootstrapping, framework installation, file transfer, workspace sync, remote execution — flows through a single channel: **direct SSH over a public TCP port**.

```
┌──────────────┐        SSH (direct TCP)        ┌───────────────┐
│  local: swm  │ ──────────────────────────────> │  remote: pod  │
│              │  exec, scp                      │  (GPU + sshd) │
│              │ <────────────────────────────── │               │
└──────┬───────┘                                 └───────┬───────┘
       │                                                 │
       │  provider API (create/stop/terminate)            │  s5cmd (S3)
       v                                                 v
┌──────────────┐                                 ┌───────────────┐
│  RunPod API  │                                 │  B2 / GCS / S3│
│  Vast.ai API │     boto3 (S3 API)              │  (workspace)  │
│  Lambda API  │ <─────────────────────────────> │               │
│  AWS API     │  local preflight checks         └───────────────┘
│  GCP CLI     │
│  k8s API     │
└──────────────┘
```

**Why SSH, not a pod-side agent?**

- No credentials stored on the pod. Storage keys are passed as transient environment variables on each s5cmd invocation — nothing is written to the pod's filesystem.
- No custom Docker images or entrypoint overrides. The provider's stock image runs unmodified.
- Any pod that has `sshd` is compatible. Adding new providers is straightforward.
- Full interactive visibility. Bootstrap output streams to your terminal in real time.

## How Pods Get sshd

When `swm pod create` runs, it injects the user's SSH **public key** as the `PUBLIC_KEY` environment variable via the provider API. The provider's stock Docker image (e.g., RunPod PyTorch) has a `/start.sh` that reads `PUBLIC_KEY`, writes it to `~/.ssh/authorized_keys`, and starts `sshd`. The pod then exposes port 22 via a public TCP mapping.

`swm` waits for the direct SSH port to become reachable (not just the API status to say "running"), then connects and runs all bootstrap steps over that connection.

## Provider Abstraction

All GPU providers implement a common interface (`CloudProvider`):

```
CloudProvider (ABC)
├── RunPodProvider      — GraphQL API via httpx
├── VastAIProvider      — REST API via httpx
├── LambdaLabsProvider  — REST API via httpx
├── AWSProvider         — boto3 SDK
├── GCPProvider         — gcloud CLI via subprocess
└── CoreWeaveProvider   — Kubernetes API via python client
```

Each provider normalizes its data into shared dataclasses (`Instance`, `GpuInfo`, `CreateConfig`) so the CLI and bootstrap logic never deal with provider-specific details.

## Storage Abstraction

All storage providers share a unified S3-compatible interface via `boto3`:

```
StorageProvider (ABC)
└── S3CompatProvider (base)    — boto3 S3 client (ls, upload, download, list_buckets)
    ├── B2Provider             — Backblaze B2 (endpoint: s3.<region>.backblazeb2.com)
    ├── GCSProvider            — Google Cloud Storage (endpoint: storage.googleapis.com)
    └── S3Provider             — Amazon S3 (native, no custom endpoint)
```

Every storage provider uses the **same S3 API** under the hood. This means:
- One code path for `ls`, `upload`, `download`, `list_buckets` across all providers
- No CLI tools needed for data operations (`b2`, `gcloud`, `aws` CLIs are not required)
- Consistent behavior and performance everywhere
- Local preflight checks (workspace size) run via the same S3 client — no SSH to the pod needed

Storage is used for two things:
1. **Workspace persistence** — your `/workspace` directory is synced to/from a cloud bucket via s5cmd on the pod (massively parallel S3 transfers).
2. **Direct bucket operations** — `swm storage ls`, `swm storage upload`, etc. for managing bucket contents from your local machine.

## Workspace Lifecycle

Workspaces are named directories inside a storage bucket (e.g., `b2:my-bucket/workspace`, `b2:my-bucket/workspace2`). The lifecycle:

1. **Create** — `swm pod create` picks the next available workspace name (or restores a specified one).
2. **Preflight** — Before pulling, `swm` queries the workspace size locally via S3 API and compares against the pod's volume size (from provider metadata). If it won't fit, shows a per-directory breakdown.
3. **Pull** — After the pod is online, s5cmd copies the workspace from the bucket to `/workspace` on the pod. Non-destructive (`s5cmd cp --no-clobber` — skips existing files). Progress bar with speed, ETA, and file counts.
4. **Work** — Run ComfyUI, generate videos, train models, etc.
5. **Push** — `swm pod down` copies `/workspace` back to the bucket before terminating. Also non-destructive.
6. **Resume** — `swm pod create -w workspace2` restores a previous workspace onto a new pod.

The workspace name and storage bucket are tracked in `swm config` per pod ID, so `swm sync pull`, `swm sync push`, and `swm pod down` all know which workspace belongs to which pod without the user having to specify it.

## Design Patterns

### Unified S3 Storage Layer

All storage providers (B2, GCS, S3) are accessed through a single S3-compatible interface via `boto3`. The `S3CompatProvider` base class in `storage/base.py` implements `ls`, `upload`, `download`, and `list_buckets` using the S3 API. Each concrete provider only specifies its endpoint URL and credentials:

- **B2**: endpoint auto-detected from `b2 account get`, credentials are the application key
- **GCS**: endpoint is `storage.googleapis.com`, credentials are HMAC keys
- **S3**: native AWS, credentials from config or standard boto3 chain

### Local Preflight Checks

Before pulling a workspace, `swm sync pull` runs a preflight check entirely on the local machine:
- **Workspace size**: queried via the S3 `ListObjectsV2` paginator (no SSH needed)
- **Pod disk size**: read from the `Instance.volume_gb` metadata (no `df` over SSH)

If the workspace won't fit, it shows a per-directory size breakdown and lets the user exclude directories or abort.

### Instance Resolution

Instance IDs can be specified as `provider:id` (explicit) or just `id` (auto-resolved by querying all configured providers). This is handled by `resolve_instance()` in `swm.providers`.

### Direct SSH Preference

The `_ssh_config_for()` function in `swm.remote.ssh` resolves connection parameters for an instance. It **prefers direct SSH** (public IP + TCP port mapping for port 22) over the provider's SSH relay.

This matters because:
- Direct SSH supports `scp`, `sftp`, `rsync` — the relay does not.
- Direct SSH supports standard exec channels — the relay requires interactive shell hacks.
- Direct SSH is faster (no proxy hop).

### Non-Destructive Transfers

All workspace sync operations are non-destructive:
- **Pull** uses `s5cmd cp --no-clobber` — skips files already present at the destination, zero comparison overhead.
- **Push** uses `s5cmd sync --size-only` — only uploads new/changed files (by size), never deletes.

### Credential Isolation

Storage credentials (B2 key, GCS HMAC, S3 key) are **never written to the pod's filesystem**. They are passed as environment variables on each s5cmd command invocation over SSH. The only env var injected at pod creation time is `PUBLIC_KEY` (the user's SSH public key) to enable sshd.
