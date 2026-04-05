# swm — Cloud GPU Workflow Manager

A CLI tool for managing cloud GPU instances, storage, and AI workloads (ComfyUI, SwarmUI, video generation) across multiple providers. `swm` treats remote GPU pods as disposable compute — your workspace lives in cloud storage, and `swm` orchestrates everything over standard SSH.

**Version**: 0.1.0

## Everyday Commands

```bash
# Check GPU availability & pricing across all providers
swm pod gpus

# List your running instances
swm pod list

# Create a pod — bootstraps storage and workspace automatically
swm pod create -p runpod -g h200 -n my-session
swm pod create -p vastai -g h200 -n train --gpu-count 4 --volume 500

# Install & manage frameworks
swm setup list                                    # see available frameworks
swm setup install comfyui runpod:<id>             # install a framework
swm setup start comfyui runpod:<id>               # start it
swm setup stop comfyui runpod:<id>                # stop it

# Workspace sync
swm sync pull runpod:<id>                         # pull workspace from storage
swm sync push runpod:<id>                         # push workspace to storage

# Transfer files
swm upload runpod:<id> ./model.safetensors models/
swm download runpod:<id> ComfyUI/output/video.mp4 -d ~/Downloads

# Remote access
swm ssh runpod:<id>                               # interactive SSH
swm run runpod:<id> 'nvidia-smi'                  # run a command

# Shut down — pushes workspace to storage, then terminates
swm pod down runpod:<id>
```

## First-Time Setup

```bash
# Install
pip install -e "."

# Configure a GPU provider (pick one or more)
swm config set runpod.api_key <your-key>
swm config set vastai.api_key <your-key>
swm config set lambda.api_key <your-key>

# Configure storage (pick one or more — see Storage Setup below)
swm config set b2.key_id <key-id>
swm config set b2.app_key <app-key>
swm config set b2.bucket <bucket-name>
swm config set storage.default b2:<bucket-name>
```

---

## Architecture

### Core Design Principle: SSH as the Single Control Plane

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

### How Pods Get sshd

When `swm pod create` runs, it injects the user's SSH **public key** as the `PUBLIC_KEY` environment variable via the provider API. The provider's stock Docker image (e.g., RunPod PyTorch) has a `/start.sh` that reads `PUBLIC_KEY`, writes it to `~/.ssh/authorized_keys`, and starts `sshd`. The pod then exposes port 22 via a public TCP mapping.

`swm` waits for the direct SSH port to become reachable (not just the API status to say "running"), then connects and runs all bootstrap steps over that connection.

### Provider Abstraction

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

### Storage Abstraction

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

### Workspace Lifecycle

Workspaces are named directories inside a storage bucket (e.g., `b2:my-bucket/workspace`, `b2:my-bucket/workspace2`). The lifecycle:

1. **Create** — `swm pod create` picks the next available workspace name (or restores a specified one).
2. **Preflight** — Before pulling, `swm` queries the workspace size locally via S3 API and compares against the pod's volume size (from provider metadata). If it won't fit, shows a per-directory breakdown.
3. **Pull** — After the pod is online, s5cmd copies the workspace from the bucket to `/workspace` on the pod. Non-destructive (`s5cmd cp --no-clobber` — skips existing files). Progress bar with speed, ETA, and file counts.
4. **Work** — Run ComfyUI, generate videos, train models, etc.
5. **Push** — `swm pod down` copies `/workspace` back to the bucket before terminating. Also non-destructive.
6. **Resume** — `swm pod create -w workspace2` restores a previous workspace onto a new pod.

The workspace name and storage bucket are tracked in `swm config` per pod ID, so `swm sync pull`, `swm sync push`, and `swm pod down` all know which workspace belongs to which pod without the user having to specify it.

---

## Storage Setup

All three storage providers use the S3-compatible API via `boto3`. Each provider needs its own credentials.

### Backblaze B2

B2 application keys double as S3-compatible credentials. The S3 endpoint is auto-detected.

```bash
# 1. Create an application key at https://secure.backblaze.com/app_keys.htm
# 2. Configure swm
swm config set b2.key_id <applicationKeyId>
swm config set b2.app_key <applicationKey>
swm config set b2.bucket <bucket-name>
swm config set storage.default b2:<bucket-name>

# The S3 endpoint is auto-detected on first use from the b2 CLI.
# If you don't have the b2 CLI, set it manually:
swm config set b2.s3_endpoint https://s3.us-west-004.backblazeb2.com
```

### Google Cloud Storage

GCS requires HMAC keys for S3 compatibility. These are a one-time setup per project.

```bash
# 1. Find your service account email
gcloud iam service-accounts list --project=<project-id>

# 2. Create HMAC keys (the secret is only shown once!)
gcloud storage hmac create <service-account-email> --project=<project-id>

# 3. Configure swm
swm config set gcp.project <project-id>
swm config set gcs.hmac_access <access-id>
swm config set gcs.hmac_secret <secret>
swm config set gcp.bucket <bucket-name>
```

**Note:** The HMAC secret is only displayed at creation time. Store it immediately. If lost, create a new key pair.

### Amazon S3

For S3, you can use explicit keys or the standard AWS credential chain (env vars, `~/.aws/credentials`, IAM roles).

```bash
# Option A: Explicit keys in swm config
swm config set s3.access_key <AWS_ACCESS_KEY_ID>
swm config set s3.secret_key <AWS_SECRET_ACCESS_KEY>
swm config set s3.bucket <bucket-name>

# Option B: Use standard AWS credential chain (env vars, ~/.aws/credentials)
# Just set the bucket — boto3 will find credentials automatically
swm config set s3.bucket <bucket-name>

# Optional: set region (default: us-east-1)
swm config set aws.region us-west-2
```

---

## Project Structure

```
src/swm/
├── __init__.py          # Package root, version string
├── cli.py               # Click CLI — all user-facing commands
├── config.py            # TOML config (~/.config/swm/config.toml)
├── bootstrap.py         # Remote setup: s5cmd, frameworks, workspace sync
├── pricing/
│   ├── __init__.py      # Re-exports
│   ├── providers.py     # Static GPU specs + pricing database
│   └── calculator.py    # Cost estimation engine
├── providers/
│   ├── __init__.py      # Provider registry + resolution
│   ├── base.py          # ABC + shared dataclasses
│   ├── runpod.py        # RunPod (GraphQL)
│   ├── vastai.py        # Vast.ai (REST API)
│   ├── lambda_labs.py   # Lambda Labs (REST API)
│   ├── aws.py           # AWS EC2 (boto3)
│   ├── gcp.py           # GCP Compute (gcloud CLI)
│   └── coreweave.py     # CoreWeave (Kubernetes)
├── remote/
│   ├── __init__.py      # Re-exports
│   └── ssh.py           # SSH session, SCP, key management
└── storage/
    ├── __init__.py      # Storage registry + bucket resolution
    ├── base.py          # ABC + S3CompatProvider (boto3 base)
    ├── b2.py            # Backblaze B2 (S3-compatible)
    ├── gcs.py           # Google Cloud Storage (S3-compatible)
    └── s3.py            # Amazon S3 (native)
```

---

## CLI Reference

### Global

| Command | Description |
|---------|-------------|
| `swm --version` | Show version |
| `swm ssh <instance_id>` | Open interactive SSH session |
| `swm run <instance_id> <command>` | Execute a command remotely |
| `swm upload <instance_id> <local> [remote]` | SCP file/dir to pod |
| `swm download <instance_id> <remote> [-d dir]` | SCP file/dir from pod |

### `swm config`

| Command | Description |
|---------|-------------|
| `swm config set <key> <value>` | Set a config value |
| `swm config get <key>` | Read a config value |
| `swm config list` | Show all config |
| `swm config delete <key>` | Remove a key |
| `swm config path` | Show config file location |

### `swm pod`

| Command | Description |
|---------|-------------|
| `swm pod create -p <provider> -g <gpu> -n <name>` | Provision + bootstrap |
| `swm pod list` | List instances across providers |
| `swm pod status <id>` | Detailed instance info |
| `swm pod start <id>` | Resume a stopped instance |
| `swm pod stop <id>` | Stop (preserves volume) |
| `swm pod down <id>` | Push workspace + terminate |
| `swm pod terminate <id>` | Destroy instance + volume |
| `swm pod gpus` | Show available GPUs (live + static) |

### `swm sync`

| Command | Description |
|---------|-------------|
| `swm sync pull <id> [path]` | Pull workspace/subdir from storage to pod |
| `swm sync push <id> [path]` | Push workspace/subdir from pod to storage |
| `swm sync status <id>` | Show storage sync status on pod |

### `swm setup`

| Command | Description |
|---------|-------------|
| `swm setup list` | List available frameworks |
| `swm setup install <framework> <id>` | Install a framework (comfyui, swarmui, axolotl, llm-studio) |
| `swm setup start <framework> <id>` | Start a framework in the background |
| `swm setup stop <framework> <id>` | Stop a running framework |
| `swm setup storage <id>` | Install s5cmd + verify S3 connection |

### `swm storage`

| Command | Description |
|---------|-------------|
| `swm storage list` | List buckets |
| `swm storage create <name> -p <provider>` | Create a bucket |
| `swm storage ls [path]` | List bucket contents |
| `swm storage upload <local> <remote>` | Upload to bucket |
| `swm storage download <remote> <local>` | Download from bucket |

### `swm pricing`

| Command | Description |
|---------|-------------|
| `swm pricing compare` | Side-by-side GPU pricing table |
| `swm pricing estimate --gpu h200 --hours 3` | Monthly cost estimate |
| `swm pricing specs` | GPU hardware specs comparison |

---

## Configuration Reference

Config lives at `~/.config/swm/config.toml`. All values are set via `swm config set <key> <value>`.

### Provider Credentials

| Key | Description |
|-----|-------------|
| `runpod.api_key` | RunPod API key |
| `aws.region` | AWS region (default: `us-east-1`) |
| `aws.ami` | Custom AMI ID for EC2 instances |
| `aws.key_name` | EC2 key pair name |
| `aws.subnet_id` | VPC subnet ID |
| `aws.security_group` | Security group ID |
| `gcp.project` | GCP project ID |
| `gcp.zone` | GCP zone (e.g., `us-central1-a`) |
| `coreweave.kubeconfig` | Path to CoreWeave kubeconfig |
| `coreweave.namespace` | Kubernetes namespace |
| `vastai.api_key` | Vast.ai API key |
| `lambda.api_key` | Lambda Labs API key |

### Storage (S3-Compatible)

All storage providers use the S3 API via boto3.

| Key | Description |
|-----|-------------|
| `b2.key_id` | Backblaze B2 application key ID (= S3 access key) |
| `b2.app_key` | Backblaze B2 application key (= S3 secret key) |
| `b2.bucket` | Default B2 bucket name |
| `b2.s3_endpoint` | B2 S3 endpoint (auto-detected, e.g., `https://s3.us-west-004.backblazeb2.com`) |
| `gcs.hmac_access` | GCS HMAC access ID (from `gcloud storage hmac create`) |
| `gcs.hmac_secret` | GCS HMAC secret (from `gcloud storage hmac create`) |
| `gcp.bucket` | Default GCS bucket name |
| `s3.access_key` | AWS access key ID (optional if using env/profile) |
| `s3.secret_key` | AWS secret access key (optional if using env/profile) |
| `s3.bucket` | Default S3 bucket name |
| `storage.default` | Default storage in `provider:bucket` format (e.g., `b2:my-bucket`) |

### SSH

| Key | Description |
|-----|-------------|
| `ssh.key_path` | Path to SSH private key (default: auto-detect from `~/.ssh/`) |
| `<provider>.ssh_key` | Per-provider SSH key path override |
| `<provider>.ssh_user` | Per-provider SSH user override |

### Pod Metadata (auto-managed)

These are set automatically by `swm pod create` and cleaned up by `swm pod down`:

| Key | Description |
|-----|-------------|
| `pods.<id>.provider` | Provider slug |
| `pods.<id>.name` | Pod name |
| `pods.<id>.workspace` | Workspace name in bucket |
| `pods.<id>.storage` | Storage spec (`b2:bucket-name`) |

---

## Design Patterns

### 1. Unified S3 Storage Layer

All storage providers (B2, GCS, S3) are accessed through a single S3-compatible interface via `boto3`. The `S3CompatProvider` base class in `storage/base.py` implements `ls`, `upload`, `download`, and `list_buckets` using the S3 API. Each concrete provider only specifies its endpoint URL and credentials:

- **B2**: endpoint auto-detected from `b2 account get`, credentials are the application key
- **GCS**: endpoint is `storage.googleapis.com`, credentials are HMAC keys
- **S3**: native AWS, credentials from config or standard boto3 chain

This eliminates the need for three separate CLIs (`b2`, `gcloud`, `aws`) for data operations.

### 2. Local Preflight Checks

Before pulling a workspace, `swm sync pull` runs a preflight check entirely on the local machine:
- **Workspace size**: queried via the S3 `ListObjectsV2` paginator (no SSH needed)
- **Pod disk size**: read from the `Instance.volume_gb` metadata (no `df` over SSH)

If the workspace won't fit, it shows a per-directory size breakdown and lets the user exclude directories or abort.

### 3. Instance Resolution

Instance IDs can be specified as `provider:id` (explicit) or just `id` (auto-resolved by querying all configured providers). This is handled by `resolve_instance()` in `swm.providers`.

### 4. Direct SSH Preference

The `_ssh_config_for()` function in `swm.remote.ssh` resolves connection parameters for an instance. It **prefers direct SSH** (public IP + TCP port mapping for port 22) over the provider's SSH relay.

This matters because:
- Direct SSH supports `scp`, `sftp`, `rsync` — the relay does not.
- Direct SSH supports standard exec channels — the relay requires interactive shell hacks.
- Direct SSH is faster (no proxy hop).

### 5. Non-Destructive Transfers

All workspace sync operations are non-destructive:
- **Pull** uses `s5cmd cp --no-clobber` — skips files already present at the destination, zero comparison overhead.
- **Push** uses `s5cmd sync --size-only` — only uploads new/changed files (by size), never deletes.

### 6. Rich Progress for Transfers

Workspace pull/push operations use `s5cmd --json` which emits one JSON line per completed file. A `line_callback` on the SSH exec channel parses these and drives a Rich progress bar with:
- Progress bar + percentage
- Downloaded/total bytes
- Transfer speed (computed from bytes over time)
- ETA
- File counts (copied + skipped)

### 7. Credential Isolation

Storage credentials (B2 key, GCS HMAC, S3 key) are **never written to the pod's filesystem**. They are passed as environment variables on each s5cmd command invocation over SSH. The only env var injected at pod creation time is `PUBLIC_KEY` (the user's SSH public key) to enable sshd.

---

## Adding a New GPU Provider

To add support for a new provider (e.g., Nebius, Cudo Compute):

### 1. Create the provider module

Create `src/swm/providers/nebius.py`:

```python
from swm.providers.base import (
    CloudProvider, CreateConfig, GpuInfo, Instance, InstanceStatus,
)

class NebiusProvider(CloudProvider):
    @property
    def name(self) -> str:
        return "Nebius"

    @property
    def slug(self) -> str:
        return "nebius"

    def is_configured(self) -> bool:
        from swm import config as cfg
        return cfg.get("nebius.api_key") is not None

    def list_instances(self) -> list[Instance]: ...
    def create_instance(self, config: CreateConfig) -> Instance: ...
    def start_instance(self, instance_id: str) -> Instance: ...
    def stop_instance(self, instance_id: str) -> Instance: ...
    def terminate_instance(self, instance_id: str) -> bool: ...
    def list_gpus(self) -> list[GpuInfo]: ...
```

### 2. Register it

In `src/swm/providers/__init__.py`:

```python
from swm.providers.nebius import NebiusProvider
ALL_PROVIDERS: list[type[CloudProvider]] = [..., NebiusProvider]
```

### 3. Requirements for SSH compatibility

| Requirement | Why |
|-------------|-----|
| **Public IP + TCP port for SSH** | Direct SCP/rsync file transfers |
| **sshd inside the container** | The provider's Docker image must start sshd |
| **Environment variable injection** | `swm` passes `PUBLIC_KEY` at creation time |
| **Stop/resume** (optional) | Enables `swm pod stop` / `swm pod start` |

---

## Adding a New Storage Backend

Any S3-compatible storage provider can be added in minutes.

### 1. Create the storage module

Create `src/swm/storage/r2.py`:

```python
from swm import config as cfg
from swm.storage.base import BucketInfo, S3CompatProvider

class R2Provider(S3CompatProvider):
    _bucket_config_key = "r2.bucket"

    @property
    def name(self) -> str:
        return "Cloudflare R2"

    @property
    def slug(self) -> str:
        return "r2"

    def is_configured(self) -> bool:
        return bool(cfg.get("r2.access_key") and cfg.get("r2.secret_key"))

    def _s3_endpoint_url(self) -> str | None:
        account_id = cfg.get("r2.account_id")
        return f"https://{account_id}.r2.cloudflarestorage.com"

    def _s3_credentials(self) -> tuple[str | None, str | None]:
        return str(cfg.get("r2.access_key")), str(cfg.get("r2.secret_key"))

    def create_bucket(self, name, location="", storage_class="") -> BucketInfo:
        self.s3.create_bucket(Bucket=name)
        cfg.set_value("r2.bucket", name)
        return BucketInfo(provider=self.slug, name=name)
```

### 2. Register it

In `src/swm/storage/__init__.py`:

```python
from swm.storage.r2 import R2Provider
ALL_STORAGE: list[type[StorageProvider]] = [..., R2Provider]
```

### 3. Add s5cmd support

In `src/swm/bootstrap.py`, add the provider's slug to `_s3_env()` so it can build the correct env vars. Then wire it into `swm setup storage` and `swm pod create`'s bootstrap flow in `cli.py`.

---

## Dependencies

### Core (always installed)

| Package | Purpose |
|---------|---------|
| `click>=8.1` | CLI framework |
| `rich>=13.0` | Terminal UI (tables, colors, progress) |
| `tomli_w>=1.0` | TOML writing for config |
| `httpx>=0.27` | HTTP client for RunPod GraphQL API |
| `boto3>=1.34` | S3-compatible API for all storage providers |

### Optional

| Extra | Package | Purpose |
|-------|---------|---------|
| `coreweave` | `kubernetes>=29.0` | CoreWeave provider |
| `b2` | `b2>=4.0` | Backblaze B2 CLI (only for `b2 account get` endpoint auto-detect) |

### System tools (not pip-managed)

| Tool | Required by | Purpose |
|------|-------------|---------|
| `ssh`, `scp` | `swm.remote.ssh` | All remote operations |
| `gcloud` | `swm.providers.gcp`, `swm.storage.gcs` (bucket creation only) | GCP compute + bucket creation |
| `s5cmd` | `swm.bootstrap` | Workspace sync (installed on pod automatically) |

---

## Security Model

- **SSH key authentication only** — no passwords stored anywhere.
- **No credentials on pods** — storage keys are passed over SSH at setup time, never in pod env vars.
- **Config file permissions** — `~/.config/swm/config.toml` stores API keys and should be protected by filesystem permissions.
- **Non-destructive syncs** — `s5cmd cp --no-clobber` (pull) and `s5cmd sync --size-only` (push) ensure data is never deleted.
- **Secure cloud default** — `swm pod create` defaults to `--cloud-type SECURE` on RunPod.
- **S3 over HTTPS** — all storage API calls use TLS endpoints.
