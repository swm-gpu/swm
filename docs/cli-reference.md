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
| `--sort [price\|vram\|provider]` | Sort order (default: price) |
| `-n, --limit N` | Max rows to show (default: 20) |
| `--all` | Show all results |

```bash
swm gpus                              # everything
swm gpus -g h200                      # filter GPU type
swm gpus -g h200 -c 4 --secure       # 4x H200, secure clouds only
swm gpus --max-price 4 -p vastai     # under $4/hr on Vast.ai
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
| `--cloud-type TEXT` | Cloud type: SECURE, COMMUNITY, ALL (default: SECURE) |
| `--ports TEXT` | Ports to expose (default: 22/tcp,8888/http,8188/http) |
| `--gpu-count N` | Number of GPUs (default: 1) |
| `--region TEXT` | Datacenter/region ID |
| `-x, --exclude PATTERN` | Glob pattern to exclude from pull (repeatable) |
| `-y, --yes` | Skip confirmation |

```bash
swm pod create -p runpod -g h200 -n my-session
swm pod create -p vastai -g h200 -n train --gpu-count 4 --volume 500
swm pod create -p runpod -g b200 -n gen -w my-workspace  # restore workspace
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

### `swm sync watch <id>`

Start the filesystem change watcher for automatic push-on-change.

| Option | Description |
|--------|-------------|
| `--stop` | Stop the watcher instead of starting it |

### `swm sync status <id>`

Show storage sync status (last push timestamp, watcher state, pending changes).

## `swm setup`

Install, start, and stop AI frameworks on running instances.

### `swm setup list`

Show all available frameworks.

### `swm setup install <framework> <id>`

Install a framework on a running instance.

Supported frameworks: `comfyui`, `swarmui`, `axolotl`, `llm-studio`

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
