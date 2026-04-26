# CLI Reference

## Global Commands

| Command | Description |
|---------|-------------|
| `swm --version` | Show version |
| `swm gpus` | Search live GPU availability and pricing |
| `swm ssh <id>` | Open interactive SSH session |
| `swm run <id> <command>` | Execute a command on a remote instance |
| `swm upload <id> <local> [remote]` | Upload a file or directory to an instance |
| `swm download <id> <remote> [-d dir]` | Download a file or directory from an instance |

## `swm gpus`

Search live GPU availability and pricing across all configured providers.

| Option | Description |
|--------|-------------|
| `-g, --gpu TEXT` | Filter by GPU name (free text: h200, a100, rtx4090) |
| `-c, --count N` | GPU count (e.g. 4 for 4-GPU configs) |
| `--max-price N` | Maximum on-demand price per hour |
| `-p, --provider TEXT` | Filter to one provider |
| `--secure` | Only show secure/certified cloud providers |
| `-r, --region TEXT` | Filter by region (free text, e.g. us-east) |
| `--sort [price\|vram\|provider]` | Sort order (default: price) |
| `-n, --limit N` | Max rows to show (default: 20) |
| `--all` | Show all results |

The output table includes a **Min CUDA** column showing each GPU's minimum CUDA toolkit (e.g. Hopper → 11.8, Blackwell → 12.8). When all rows in the result share the same minimum, swm appends `--cuda <X.Y>` to the suggested next-step `swm pod create` command.

```bash
swm gpus                              # everything
swm gpus -g h200                      # filter GPU type
swm gpus -g h200 -c 4 --secure       # 4x H200, secure clouds only
swm gpus --max-price 4 -p vastai     # under $4/hr on Vast.ai
swm gpus -r us-west                  # filter by region
```

## `swm pod`

Manage GPU instances across providers.

### `swm pod create`

Provision a new GPU instance with automatic workspace sync.

| Option | Description |
|--------|-------------|
| `-p, --provider` | Cloud provider (required) |
| `-g, --gpu TEXT` | GPU type (default: h200) |
| `-n, --name TEXT` | Instance name (required) |
| `-w, --workspace TEXT` | Restore an existing workspace (creates new if omitted) |
| `-b, --bucket TEXT` | Storage bucket override (provider:bucket) |
| `--no-storage` | Skip automatic storage setup |
| `--volume N` | Persistent volume size in GB (default: 100) |
| `--disk N` | Container disk size in GB (default: 40) |
| `--image TEXT` | Docker image (provider default if empty) |
| `--cuda X.Y` | Auto-pick newest provider image matching this CUDA major.minor (e.g. `12.8`). Ignored if `--image` is set. |
| `--cloud-type TEXT` | Cloud type: SECURE, COMMUNITY, ALL (default: SECURE) |
| `--ports TEXT` | Ports to expose (default: 22/tcp,8888/http,8188/http) |
| `--gpu-count N` | Number of GPUs (default: 1) |
| `--region TEXT` | Datacenter/region ID |
| `--lifecycle [manual\|remind\|auto-stop\|auto-down]` | Idle lifecycle policy for this pod (configures the guard) |
| `--idle-timeout N` | Idle timeout in minutes (used with `--lifecycle`) |
| `-x, --exclude PATTERN` | Glob pattern to exclude from pull (repeatable) |
| `-y, --yes` | Skip confirmation |

If a workspace is configured, `swm pod create` runs the full bootstrap over SSH (install s5cmd, configure storage, pull workspace, start the auto-sync daemon). On any partial failure (SSH probe timeout, storage misconfigured, pull aborted, …) it persists the pod ↔ workspace mapping unconditionally and prints exactly the swm commands you need to retry the missing steps:

```
⚠ Bootstrap incomplete. Re-run the remaining steps when ready:
  Storage configuration:  swm setup storage runpod:abc123
  Workspace pull:         swm sync pull runpod:abc123
  Auto-sync start:        swm sync auto runpod:abc123
```

```bash
swm pod create -p runpod -g h200 -n my-session
swm pod create -p runpod -g h200 -n my-session --cuda 12.8
swm pod create -p vastai -g h200 -n train --gpu-count 4 --volume 500
swm pod create -p runpod -g b200 -n gen -w my-workspace          # restore workspace
swm pod create -p runpod -g h100 -n train --lifecycle auto-down --idle-timeout 30
```

### `swm pod list`

| Option | Description |
|--------|-------------|
| `-p, --provider TEXT` | Filter to one provider |

### `swm pod status <id>`

Show detailed status, GPU info, SSH connection details, and ports.

### `swm pod start <id>`

Resume a stopped instance. Accepts `provider:id` or bare id.

### `swm pod stop <id>`

Stop a running instance (preserves volume, pauses billing).

### `swm pod down <id>`

Push workspace to storage and terminate. Non-destructive — uploads changed files, then destroys the pod.

| Option | Description |
|--------|-------------|
| `-y, --yes` | Skip confirmation |
| `--no-sync` | Skip workspace push before termination |
| `-x, --exclude PATTERN` | Glob pattern to exclude from push (repeatable) |

### `swm pod terminate <id>`

Destroy an instance and delete its volume. Irreversible.

| Option | Description |
|--------|-------------|
| `-y, --yes` | Skip confirmation |

## `swm sync`

Sync files between cloud storage and running instances.

### `swm sync pull <id> [path]`

Pull workspace (or a subdirectory) from cloud storage to the pod.

| Option | Description |
|--------|-------------|
| `-b, --bucket TEXT` | Override bucket |
| `-d, --dest TEXT` | Destination on pod (default: /workspace) |
| `-x, --exclude PATTERN` | Glob pattern to exclude (repeatable) |
| `-f, --force` | Kill any running transfer and start fresh |

### `swm sync push <id> [path]`

Push workspace from the pod to cloud storage.

| Option | Description |
|--------|-------------|
| `-b, --bucket TEXT` | Override bucket |
| `-d, --dest TEXT` | Override destination in bucket |
| `-x, --exclude PATTERN` | Glob pattern to exclude (repeatable) |
| `-f, --force` | Kill any running transfer and start fresh |
| `--tar` | Pack into a single compressed tarball before uploading (faster for many small files) |
| `--delete` | Also delete files from storage that were deleted locally. Requires the watcher to be running so swm has an authoritative log of deletions. |

By default, push is **non-destructive** — files removed locally are left untouched in the bucket. `--delete` opts in to mirror semantics. swm refuses `--delete` if the watcher isn't running so a missed change can't accidentally wipe storage.

### `swm sync watch <id>`

Start the filesystem change watcher for automatic push-on-change.

| Option | Description |
|--------|-------------|
| `--stop` | Stop the watcher instead of starting it |

### `swm sync auto <id>`

Start a background daemon that auto-syncs `/workspace` every interval. Reads the watcher log, uploads changed files, and removes deleted files from storage.

| Option | Description |
|--------|-------------|
| `-i, --interval N` | Sync interval in seconds (default: 60) |
| `-b, --bucket TEXT` | Override bucket |
| `-d, --dest TEXT` | Override destination path inside bucket |
| `--status` | Show daemon status and recent log |
| `--stop` | Stop the daemon |
| `--force` | Bypass the safety check that requires a prior successful pull/push (DANGEROUS — local deletions will propagate) |

The daemon is started automatically by `swm pod create` when a workspace is configured. **Safety:** it refuses to start unless a prior `swm sync pull` or `swm sync push` succeeded (marked by a push stamp on the pod). Without that signal, a stray local deletion would propagate to storage and erase the remote copy.

```bash
swm sync auto runpod:abc123              # start with 60s interval
swm sync auto runpod:abc123 -i 30        # 30s interval
swm sync auto runpod:abc123 --status     # check daemon + recent log
swm sync auto runpod:abc123 --stop       # stop the daemon
```

### `swm sync status <id>`

Show storage sync status (last push timestamp, watcher state, pending changes).

## `swm setup`

Install, start, and stop AI frameworks on running instances.

### `swm setup list`

Show all available frameworks.

### `swm setup install <framework> <id>`

Install a framework on a running instance.

Supported frameworks: `vllm`, `open-webui`, `ollama`, `comfyui`, `swarmui`, `axolotl`, `llm-studio`.

Run `swm setup list` to see the live list with categories, default ports, and recommended pairings.

### `swm setup start <framework> <id>`

Start a framework in the background.

| Option | Description |
|--------|-------------|
| `-p, --port N` | Override the default listen port |

### `swm setup stop <framework> <id>`

Stop a running framework.

### `swm setup storage <id>`

Install s5cmd and verify storage connectivity on an instance.

| Option | Description |
|--------|-------------|
| `-p, --provider TEXT` | Storage backend: b2, gcs, s3, all (default: all) |

### `swm setup workspace <id>`

Attach an object-storage workspace to an existing pod in one command. Performs the full bootstrap: install s5cmd, configure storage, pull (or initialize) the workspace, persist the pod ↔ workspace ↔ bucket mapping in config, and start the auto-sync daemon.

| Option | Description |
|--------|-------------|
| `-n, --name TEXT` | Workspace name (default: pod name) |
| `-b, --bucket TEXT` | Bucket spec `provider:bucket` (default: configured) |
| `--force` | Reattach a pod that already has a workspace tracked |

Use this when:

- You created a pod with `--no-storage` and want to add a workspace later.
- `swm pod create`'s SSH probe timed out and the bootstrap was skipped.
- You want to reattach a pod to a different workspace (`--force`).

```bash
swm setup workspace runpod:abc123                   # ws name = pod name
swm setup workspace runpod:abc123 -n my-ws          # custom name
swm setup workspace runpod:abc123 -b b2:my-bucket   # explicit bucket
swm setup workspace runpod:abc123 --force           # reattach
```

## `swm costs`

Track GPU spending and compare with provider billing.

### `swm costs summary`

Spending breakdown grouped by provider and GPU type.

| Option | Description |
|--------|-------------|
| `-t, --period` | Time window: today, week, month, all (default: month) |
| `-p, --provider TEXT` | Filter to one provider |

### `swm costs log`

Detailed session log showing every pod run.

| Option | Description |
|--------|-------------|
| `-n, --limit N` | Number of sessions to show (default: 20) |
| `-p, --provider TEXT` | Filter to one provider |

### `swm costs live`

Show running cost of all active pods in real time.

### `swm costs reconcile`

Query provider billing APIs to show card charges, GPU usage breakdown, and compare against local tracking.

| Option | Description |
|--------|-------------|
| `-p, --provider TEXT` | Reconcile one provider only (runpod, vastai) |

### `swm costs budget set <amount>`

Set a spending budget with alerts at 80% and 100%.

| Option | Description |
|--------|-------------|
| `-s, --scope TEXT` | Budget scope: global, provider:\<slug\>, pod:\<id\> (default: global) |
| `-t, --period` | Budget period: daily, weekly, monthly, total (default: monthly) |

```bash
swm costs budget set 50                           # $50/month global
swm costs budget set 100 --scope provider:runpod  # per-provider
swm costs budget set 10 --period daily            # daily cap
```

### `swm costs budget show`

Show all active budgets with current spend and progress bars.

### `swm costs budget remove <scope>`

Remove a budget.

| Option | Description |
|--------|-------------|
| `-t, --period` | Budget period (default: monthly) |

## `swm images`

List Docker images available to `swm pod create --image` for a given provider, parsed live from the provider's image registry. Useful for picking a tag with a specific CUDA version.

### `swm images list`

| Option | Description |
|--------|-------------|
| `-p, --provider TEXT` | Provider to query (default: runpod) |
| `--cuda X.Y` | Filter to images matching CUDA major.minor |
| `--refresh` | Bypass the local cache and re-query the registry |
| `-n, --limit N` | Max rows to show |

```bash
swm images list                                 # all RunPod pytorch images
swm images list -p runpod --cuda 12.8           # CUDA 12.8 images only
swm images list -p runpod --cuda 12.8 -n 5      # top 5
```

`swm pod create --cuda 12.8` resolves to the newest image returned here.

## `swm guard`

Lifecycle automation. Monitors SSH sessions, GPU utilization, filesystem writes, transfers, and active processes; reminds, stops, or terminates an idle pod according to its policy.

### `swm guard defaults`

Show or update the global default policy.

| Option | Description |
|--------|-------------|
| `--mode [manual\|remind\|auto-stop\|auto-down]` | Default mode |
| `--idle-timeout N` | Default idle timeout in minutes |
| `--poll-interval N` | Default on-pod watcher poll interval in seconds |

### `swm guard set <id>`

Configure the policy for a single pod.

| Option | Description |
|--------|-------------|
| `--mode [manual\|remind\|auto-stop\|auto-down]` | Required |
| `--idle-timeout N` | Idle timeout in minutes |
| `--poll-interval N` | On-pod watcher poll interval in seconds |

### `swm guard disable <id>`

Remove the per-pod policy (falls back to defaults).

### `swm guard list`

List guarded pods with current idle time, policy, and remote daemon status.

### `swm guard run [ids...]`

Run a guard cycle manually for one or more pods. Without IDs, evaluates every guarded pod.

| Option | Description |
|--------|-------------|
| `--once` | Run a single cycle and exit (default loops every interval) |
| `--interval N` | Loop interval in seconds |

### `swm guard stop-daemon`

Stop the local background `swm guard run` daemon.

```bash
swm guard defaults --mode auto-down --idle-timeout 60
swm guard set runpod:abc123 --mode auto-down --idle-timeout 30
swm guard list
swm guard run --once
```

## `swm models`

Search HuggingFace Hub, download models to pods, and hot-swap the active vLLM model. Supports both HuggingFace (`org/model-name`) and Ollama (`model:tag`).

### `swm models search <query>`

| Option | Description |
|--------|-------------|
| `--sort [downloads\|likes\|trending]` | Sort order |
| `-n, --limit N` | Max results |

### `swm models info <model>`

Detailed metadata for a single model (size, license, tags).

### `swm models pull <id> <model>`

Download a model to the pod's `/workspace/models` directory.

| Option | Description |
|--------|-------------|
| `--token TEXT` | HuggingFace token (overrides `swm config set hf_token`) |

### `swm models set <id> <model>`

Activate a model for vLLM (writes the launch config). By default this restarts vLLM so the new model takes effect immediately.

| Option | Description |
|--------|-------------|
| `--no-restart` | Update the active-model file but leave vLLM running with the old model |

### `swm models list <id>`

List models already downloaded on the pod.

### `swm models remove <id> <model>`

Delete a model from the pod.

```bash
swm models search "qwen3 coder" --sort downloads
swm models info Qwen/Qwen3-235B-A22B
swm models pull runpod:abc123 Qwen/Qwen3-8B
swm models set runpod:abc123 Qwen/Qwen3-8B --restart
```

## `swm storage`

Manage cloud storage buckets directly.

### `swm storage list`

List buckets across configured storage providers.

| Option | Description |
|--------|-------------|
| `-p, --provider TEXT` | Filter: gcs, b2, s3 |

### `swm storage create <name>`

Create a new storage bucket.

| Option | Description |
|--------|-------------|
| `-p, --provider TEXT` | Storage provider (required) |
| `-l, --location TEXT` | Bucket location/region |
| `-c, --storage-class TEXT` | Storage class |

### `swm storage ls [path]`

List contents of a bucket.

| Option | Description |
|--------|-------------|
| `-b, --bucket TEXT` | Bucket (default from config) |

### `swm storage upload <local> <remote>`

Upload a file to a bucket.

| Option | Description |
|--------|-------------|
| `-b, --bucket TEXT` | Target bucket |

### `swm storage download <remote> <local>`

Download a file from a bucket.

| Option | Description |
|--------|-------------|
| `-b, --bucket TEXT` | Source bucket |

## `swm config`

Manage configuration (API keys, defaults, preferences).

| Command | Description |
|---------|-------------|
| `swm config set <key> <value>` | Set a config value |
| `swm config get <key>` | Read a config value |
| `swm config list` | Show all config |
| `swm config delete <key>` | Remove a key |
| `swm config path` | Show config file location |

## `swm pricing`

Static GPU pricing comparison and cost estimation.

### `swm pricing compare`

Side-by-side per-GPU/hr pricing table.

| Option | Description |
|--------|-------------|
| `--gpu [h200\|b200]` | Filter by GPU type |
| `--single-gpu` | Only show single-GPU configs |

### `swm pricing estimate`

Estimate monthly cost for a workload.

| Option | Description |
|--------|-------------|
| `--gpu [h200\|b200]` | GPU type (default: h200) |
| `--hours N` | Hours per week (default: 3) |
| `--storage N` | Model storage in GB (default: 100) |
| `--provider TEXT` | Filter to one provider |
| `--single-gpu` | Only single-GPU configs |
| `--tier [on_demand\|spot\|reserved]` | Pricing tier (default: on_demand) |

### `swm pricing specs`

GPU hardware specs comparison (VRAM, bandwidth, TDP, etc.).

| Option | Description |
|--------|-------------|
| `--gpu [h200\|b200]` | Filter by GPU type |

## Remote Access

### `swm ssh <id>`

Open an interactive SSH session to a running instance.

### `swm run <id> <command>`

Run a command on a remote instance and stream the output.

| Option | Description |
|--------|-------------|
| `-q, --quiet` | Suppress real-time output |

### `swm upload <id> <local> [remote]`

Upload a file or directory to a running instance.

| Option | Description |
|--------|-------------|
| `-r, --recursive` | Upload a directory recursively |

### `swm download <id> <remote>`

Download a file or directory from a running instance.

| Option | Description |
|--------|-------------|
| `-d, --dir PATH` | Local directory to save into (default: current dir) |

## Instance ID Format

Instances are referenced as `provider:id`:

```
runpod:abc123
vastai:34182944
lambda:inst-xyz
vultr:vm-1234
```

A bare ID (without the provider prefix) auto-resolves by querying all configured providers.
