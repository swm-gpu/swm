# How swm works

## SSH as the control plane

swm uses **SSH** as the single communication channel to GPU pods. No custom agents, no modified Docker images, no proprietary protocols.

```
┌──────────┐       SSH (direct TCP)       ┌─────────────┐
│ Your Mac │ ──────────────────────────>  │  GPU Pod    │
│   swm    │  commands, file transfers    │  (sshd)     │
│          │ <──────────────────────────  │             │
└────┬─────┘                              └──────┬──────┘
     │                                           │
     │  Provider API                             │  s5cmd (S3 API)
     │  (create / stop / terminate)              │  (workspace sync)
     v                                           v
┌──────────────────┐                     ┌───────────────┐
│  RunPod, Vast.ai │                     │  B2 / S3 / GCS│
│  Lambda, Vultr   │                     │  (workspace)  │
│  AWS, GCP, Azure │                     └───────────────┘
│  TensorDock,     │
│  FluidStack,     │
│  CoreWeave       │
└──────────────────┘
```

Why SSH instead of an agent:

- No credentials stored on the pod — storage keys are passed as transient environment variables per command
- No custom Docker images — the provider's stock image runs unmodified
- Any pod with `sshd` is compatible — adding new providers is straightforward
- Full interactive visibility — bootstrap output streams to your terminal in real time

## Pod lifecycle

### 1. Provisioning

When you run `swm pod create`, swm:

1. Calls the provider's API to create an instance
2. Injects your SSH **public key** via the provider's environment variable mechanism
3. The provider's image starts `sshd` and writes the key to `~/.ssh/authorized_keys`
4. swm waits for the SSH port to become reachable (not just the API status)

### 2. Bootstrap

Once SSH is up, swm runs a sequence of commands over SSH:

1. **Install s5cmd** — a high-performance S3 client for parallel file transfers
2. **Configure storage** — verifies connectivity to your B2 / S3 / GCS bucket
3. **Pull workspace** — copies your workspace from cloud storage to `/workspace` on the pod
4. **Install `inotify-tools`** and launch an `inotifywait` daemon that records every file change to a log
5. **Start auto-sync** — a background daemon tails the watcher log and pushes new / changed / deleted files to storage every 60 seconds

If any step fails (SSH probe timeout, missing storage credentials, network blip, …) swm persists the pod ↔ workspace mapping anyway and prints the exact swm commands to retry the missing steps. You can re-attach the workspace later with `swm setup workspace <pod>`.

### 3. Working

The pod is now ready. You can:

- SSH in interactively with `swm ssh`
- Install frameworks with `swm setup install`
- Run commands with `swm run`
- Transfer files with `swm upload` / `swm download`
- Pull AI models with `swm models pull`

### 4. Shutdown

`swm pod down` performs a clean shutdown:

1. Pushes `/workspace` to cloud storage (only new / changed files)
2. Terminates the instance via the provider API
3. Cleans up local metadata

Your workspace persists in cloud storage. Spin up a new pod on any provider and the files are restored.

## Workspace sync

Workspaces are named directories in a cloud storage bucket (e.g. `b2:my-bucket/workspace`).

**Pull** (storage → pod) uses `s5cmd cp --no-clobber` — downloads files that don't already exist on the pod. Existing files are never overwritten.

**Push** (pod → storage) uses a three-tier strategy:

1. **Inotify watcher** — if running, the change log records exactly which files moved; push uploads only those
2. **Timestamp-based delta** — without a watcher, `find -newer` enumerates files modified since the last push
3. **Full scan** — falls back to `s5cmd cp --if-size-differ` for a complete comparison

**Continuous mode** (`swm sync auto`) — a daemon tails the watcher log every interval and pushes changed files plus removes deleted ones. It refuses to start unless a prior pull / push has confirmed pod and bucket are in sync, so a stray local deletion never wipes the bucket on first run.

### Deletion semantics

By default all sync paths are **non-destructive** — `sync push`, `sync pull`, and `pod down` never remove files from cloud storage. Deletions are opt-in:

- `swm sync push --delete` propagates local deletions (requires an active watcher so swm has an authoritative deletion log)
- `swm sync auto` propagates local deletions on every cycle (gated by the safety check above)

The watcher's exclude list is fingerprinted on the pod, so when swm is upgraded with new excludes long-lived watchers detect the drift and restart with the latest configuration on the next auto-sync cycle.

## Provider abstraction

All 10 GPU providers implement a common interface (`CloudProvider` in `src/swm/providers/base.py`). swm normalizes each provider's data into shared types:

- `GpuInfo` — GPU availability, pricing, VRAM, stock level
- `Instance` — running pod with SSH connection details
- `CreateConfig` — parameters for provisioning (GPU type, count, volume size, …)

Every swm command therefore works identically regardless of provider. `swm gpus` queries all providers in parallel; `swm pod create -p vastai` and `swm pod create -p runpod` follow the same flow.

| Slug | Provider | Notes |
|------|----------|-------|
| `runpod` | RunPod | API key; Secure Cloud + Community Cloud |
| `vastai` | Vast.ai | API key; community marketplace |
| `lambda` | Lambda Labs | API key; SOC 2 Type II |
| `vultr` | Vultr | API key |
| `tensordock` | TensorDock | API token |
| `fluidstack` | FluidStack | API key |
| `aws` | Amazon Web Services | Standard boto3 chain |
| `gcp` | Google Cloud Platform | `gcloud` CLI auth |
| `azure` | Microsoft Azure | Service principal |
| `coreweave` | CoreWeave | Kubernetes kubeconfig |

## Storage abstraction

All storage backends use the S3-compatible API via boto3 (`src/swm/storage/base.py:S3CompatProvider`). One code path handles all three — no provider-specific SDKs needed.

| Slug | Backend | Notes |
|------|---------|-------|
| `b2` | Backblaze B2 | S3 endpoint auto-detected via `b2` CLI |
| `gcs` | Google Cloud Storage | HMAC keys for S3 compatibility |
| `s3` | Amazon S3 (or any compatible) | Set `s3.endpoint_url` for R2 / MinIO / etc. |

Storage is used for:

- **Workspace persistence** — `/workspace` synced via s5cmd on the pod
- **Direct bucket operations** — `swm storage ls`, `swm storage upload`, etc.

## Cost tracking

`swm costs` records every session in a SQLite database at `~/.config/swm/costs.db`:

- `costs summary` / `costs log` query the local DB
- `costs live` aggregates running pods (provider API + DB)
- `costs reconcile` pulls billing data from supported provider APIs (RunPod, Vast.ai) and compares with local records

Budgets are advisory (alerts at 80% / 100%) and never block operations.

## Lifecycle guard

`swm guard` monitors pods for idleness and applies a policy when one is detected:

| Mode | Behavior on idle |
|------|------------------|
| `manual` | Off — no action |
| `remind` | Print a reminder; never stop |
| `auto-stop` | `swm pod stop` after the idle timeout |
| `auto-down` | `swm pod down` (push workspace + terminate) after the idle timeout |

Idle = no SSH connections, GPU utilization below the threshold, no filesystem writes, no active transfers, no busy processes. A small daemon on the pod (`swm-guard`) snapshots these signals at the configured poll interval; a local daemon on your laptop reads the snapshots over SSH and executes the policy when the idle window elapses.

## Security model

- **SSH key auth only** — no passwords, no tokens stored on pods
- **Transient credentials** — storage keys are passed as environment variables per s5cmd invocation, never written to the pod's filesystem
- **Secure cloud default** — `swm pod create -p runpod` defaults to `--cloud-type SECURE` (SOC 2 / HIPAA certified data centers)
- **TLS everywhere** — all S3 API calls use HTTPS endpoints
- **Active pod validation** — `swm use` failures auto-clear stale pod ids so commands can't accidentally target a deleted pod

## Source layout

```
src/swm/
├── cli.py                   # Click entry point; registers all command groups
├── config.py                # ~/.config/swm/config.toml reader/writer
├── bootstrap.py             # Pod bootstrap (s5cmd, storage, workspace pull)
├── bootstrap_ssh.py         # SSH readiness probing + key injection
├── bootstrap_frameworks.py  # Framework install / start / stop
├── cuda.py                  # GPU → minimum CUDA toolkit mapping
├── guard.py                 # Lifecycle daemon
├── images.py                # Provider image catalog
├── commands/                # CLI command implementations
│   ├── pod.py / sync.py / setup.py / costs.py / guard.py
│   ├── pricing.py / models.py / storage.py / config.py
│   ├── images.py / use.py / remote.py / _helpers.py
├── providers/               # 10 cloud provider integrations
│   ├── base.py              # CloudProvider abstract interface
│   ├── runpod.py vastai.py lambda_labs.py vultr.py …
├── storage/                 # 3 storage backends
│   ├── base.py              # S3CompatProvider via boto3
│   ├── b2.py gcs.py s3.py
├── sync/                    # Workspace sync internals
│   ├── push.py pull.py autosync.py watcher.py preflight.py
├── costs/                   # Cost tracking
│   ├── db.py tracker.py budget.py reconcile.py billing.py
├── models/huggingface.py    # HuggingFace Hub search
├── pricing/                 # Curated price comparison data
│   ├── providers.py         # GPU_SPECS + OFFERINGS dictionaries
│   ├── calculator.py        # Monthly cost / $/video math
├── frameworks/              # Framework registry (data-driven)
│   ├── __init__.py          # Loads each framework via importlib
│   ├── comfyui.py swarmui.py vllm_server.py axolotl.py
│   ├── ollama.py open_webui.py llm_studio.py
└── remote/ssh.py            # RemoteSession (SSH wrapper)
```
