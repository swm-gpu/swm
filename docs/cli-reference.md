# CLI Reference

Complete reference for every `swm` command. Generated against v0.1.12.

For tutorials and concept docs see [swmgpu.com](https://swmgpu.com/overview/).

## Top-level commands

| Command | Purpose |
|---------|---------|
| `swm gpus` | Search live GPU availability and pricing across providers |
| `swm pod` | Manage cloud GPU instances (create / list / start / stop / down / terminate) |
| `swm use` | Set the default pod id used by other commands |
| `swm ssh` | Open an interactive SSH session |
| `swm run` | Execute a command on a remote instance |
| `swm upload` / `swm download` | Transfer files to/from a pod |
| `swm sync` | Sync `/workspace` with cloud storage |
| `swm storage` | Manage cloud storage buckets directly |
| `swm setup` | Install / start / stop frameworks (ComfyUI, vLLM, …) |
| `swm models` | Search, download, manage AI models on pods |
| `swm guard` | Lifecycle automation for idle pods |
| `swm costs` | Track GPU spending and reconcile against provider billing |
| `swm pricing` | Compare GPU pricing and estimate monthly costs |
| `swm images` | List Docker images available to `swm pod create --image` |
| `swm config` | Manage `~/.config/swm/config.toml` |

## Instance id format

Instances are referenced as `provider:id`:

```
runpod:abc123
vastai:34182944
lambda:inst-xyz
aws:i-0123456789abcdef0
```

A bare id (e.g. `34182944`) is also accepted — swm queries each configured provider to locate it. The first match wins.

For commands that take an `[INSTANCE_ID]`, the resolution order is:

1. Explicit CLI argument
2. `$SWM_POD` environment variable
3. Active pod set via `swm use`

If none are set, swm raises a `UsageError` with a hint to run `swm use <pod_id>`.

## `swm use`

```
swm use [OPTIONS] [INSTANCE_ID]
```

Select which pod other commands should target by default.

| Option | Description |
|--------|-------------|
| `--show` | Print the currently active pod |
| `--clear` | Unset the active pod |

```bash
swm use vastai:12345678        # set
swm use 12345678               # bare id (auto-resolves)
swm use --show                 # display
swm use --clear                # forget
```

Shell completion (TAB-suggest known pod ids) — enable once per shell:

```bash
eval "$(_SWM_COMPLETE=bash_source swm)"   # bash
eval "$(_SWM_COMPLETE=zsh_source swm)"    # zsh
eval (env _SWM_COMPLETE=fish_source swm)  # fish
```

## `swm gpus`

```
swm gpus [OPTIONS]
```

Search live GPU availability and pricing across all configured providers. Queries every provider in parallel.

| Option | Description |
|--------|-------------|
| `-g, --gpu TEXT` | Filter by GPU name (free text: `h200`, `a100`, `rtx4090`) |
| `-c, --count INTEGER` | GPU count (e.g. `4` for 4×GPU configs) |
| `--max-price FLOAT` | Max on-demand $/hr per GPU |
| `-p, --provider TEXT` | Filter to one provider slug |
| `--secure` | Only show secure-cloud providers |
| `-r, --region TEXT` | Filter by region (free text, e.g. `us-east`, `europe`) |
| `--sort [price\|vram\|provider]` | Sort order (default: `price`) |
| `-n, --limit INTEGER` | Max rows (default: `20`) |
| `--all` | Show all results (no pagination) |

The output table includes a **Min CUDA** column showing each GPU's minimum CUDA toolkit (Hopper → 11.8, Blackwell → 12.8). When all rows share the same minimum, swm appends `--cuda <X.Y>` to the suggested next-step `swm pod create`.

```bash
swm gpus                          # everything
swm gpus -g h200 --secure         # h200 on secure clouds only
swm gpus -g h200 -c 4             # 4×h200 configs
swm gpus --max-price 4 -p vastai  # under $4/hr on vast.ai
swm gpus -r us-west               # us-west region only
```

## `swm pod`

### `swm pod create`

```
swm pod create [OPTIONS]
```

Provision a new GPU instance. Injects your SSH public key so the pod opens sshd on a direct TCP port. After the pod is online, swm connects over SSH to install s5cmd, configure storage, and pull the workspace.

| Option | Description |
|--------|-------------|
| `-p, --provider` | Cloud provider slug (required) |
| `-g, --gpu TEXT` | GPU type (`h200`, `b200`, etc.) |
| `-n, --name TEXT` | Instance name (required) |
| `-w, --workspace TEXT` | Existing workspace to restore (creates new if omitted) |
| `-b, --bucket TEXT` | Storage bucket override (`provider:bucket`) |
| `--no-storage` | Skip automatic storage setup |
| `--volume INTEGER` | Persistent volume size in GB |
| `--disk INTEGER` | Container disk size in GB |
| `--image TEXT` | Docker image tag (provider default if empty) |
| `--cuda TEXT` | Auto-pick newest provider image matching CUDA `X.Y` (ignored if `--image` is set) |
| `--cloud-type TEXT` | RunPod cloud type: `SECURE`, `COMMUNITY`, `ALL` (default: `SECURE`) |
| `--ports TEXT` | Ports to expose |
| `--gpu-count INTEGER` | Number of GPUs |
| `--region TEXT` | Datacenter/region id |
| `--lifecycle [manual\|remind\|auto-stop\|auto-down]` | Idle lifecycle policy for the guard |
| `--idle-timeout INTEGER` | Idle timeout in minutes for the guard |
| `-x, --exclude TEXT` | Glob pattern to exclude from workspace pull (repeatable) |
| `-y, --yes` | Skip confirmation |

If any bootstrap step fails (SSH probe timeout, storage misconfigured, pull aborted), swm persists the pod ↔ workspace mapping anyway and prints exactly the commands to retry:

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
swm pod create -p runpod -g b200 -n gen -w my-workspace
swm pod create -p runpod -g h100 -n train --lifecycle auto-down --idle-timeout 30
```

### `swm pod list`

| Option | Description |
|--------|-------------|
| `-p, --provider TEXT` | Filter to one provider slug |

### `swm pod status <id>`

Show detailed status: GPU info, SSH connection details, ports, current $/hr, workspace association.

### `swm pod start <id>`

Resume a stopped instance. Polls the provider until running, then probes SSH connectivity.

### `swm pod stop <id>`

Stop a running instance (preserves volume, pauses billing on supporting providers).

### `swm pod down <id>`

Push workspace to storage and terminate the pod in one step. Non-destructive: copies `/workspace/` to your bucket (never deletes remote files), then destroys the pod.

| Option | Description |
|--------|-------------|
| `-y, --yes` | Skip confirmation |
| `--no-sync` | Skip workspace push before termination |
| `-x, --exclude TEXT` | Glob pattern to exclude from push (repeatable) |

### `swm pod terminate <id>`

Destroy an instance and delete its volume. Irreversible.

| Option | Description |
|--------|-------------|
| `-y, --yes` | Skip confirmation |

### `swm pod prune`

Remove config entries for pods that no longer exist on any provider. Useful after manual deletions.

## `swm sync`

Sync files between cloud storage and running instances.

### `swm sync pull <id> [path]`

Pull workspace (or a subdirectory) from cloud storage to the pod. Always non-destructive (skips existing files, never deletes).

| Option | Description |
|--------|-------------|
| `-b, --bucket TEXT` | Override bucket (`provider:bucket`) |
| `-d, --dest TEXT` | Destination on pod (default: `/workspace`) |
| `-x, --exclude TEXT` | Glob pattern to exclude (repeatable) |
| `-f, --force` | Kill any running transfer and start fresh |
| `--tar` | Pull a tarball archive (pushed with `--tar`) and extract in one step |

```bash
swm sync pull runpod:abc123                          # full workspace
swm sync pull runpod:abc123 --tar                    # pull tarball
swm sync pull runpod:abc123 ComfyUI/models/          # subfolder
swm sync pull runpod:abc123 -x '.cache/*' -x 'venv/*'
```

### `swm sync push <id> [path]`

Push workspace from the pod to cloud storage. Uploads new and changed files only.

| Option | Description |
|--------|-------------|
| `-b, --bucket TEXT` | Override bucket (`provider:bucket`) |
| `-d, --dest TEXT` | Override destination path inside bucket |
| `-x, --exclude TEXT` | Glob pattern to exclude (repeatable) |
| `-f, --force` | Kill any running transfer and start fresh |
| `--tar` | Pack into a single compressed tarball before uploading (faster for many small files) |
| `--delete` | Also delete files from storage that were deleted locally (requires watcher) |

By default push is **non-destructive** — files removed locally are left untouched in the bucket. `--delete` opts in to mirror semantics; swm refuses it unless the watcher is running so a missed change can't accidentally wipe storage.

`--tar` is recommended when the workspace has 100k+ small files — it turns 600k S3 API calls into one.

### `swm sync watch <id>`

Start (or stop) the filesystem change watcher on an instance. The watcher tracks file changes in `/workspace/` so subsequent pushes only upload modified files — no scanning required.

| Option | Description |
|--------|-------------|
| `--stop` | Stop the watcher instead of starting it |

### `swm sync auto <id>`

Start a background daemon that auto-syncs `/workspace` to storage every interval. Reads the watcher log, uploads changed files, and removes deleted files from storage.

| Option | Description |
|--------|-------------|
| `-i, --interval INTEGER` | Sync interval in seconds (default: `60`) |
| `-b, --bucket TEXT` | Override bucket (`provider:bucket`) |
| `-d, --dest TEXT` | Override destination path inside bucket |
| `--status` | Show daemon status and recent log |
| `--stop` | Stop the daemon |
| `--force` | Bypass the safety check (DANGEROUS — local deletions will propagate) |

The daemon is started automatically by `swm pod create` when a workspace is configured. Safety: it refuses to start unless a prior `swm sync pull` or `swm sync push` succeeded (marked by a push stamp on the pod).

```bash
swm sync auto runpod:abc123                # start with 60s interval
swm sync auto runpod:abc123 -i 30          # 30s
swm sync auto runpod:abc123 --status       # check daemon + recent log
swm sync auto runpod:abc123 --stop         # stop the daemon
```

### `swm sync status <id>`

Show storage sync status (last push timestamp, watcher state, pending changes).

## `swm setup`

Install, start, and stop frameworks on running instances.

### `swm setup list`

Show all available frameworks with categories, default ports, and recommended pairings.

Supported frameworks: `vllm`, `open-webui`, `ollama`, `comfyui`, `swarmui`, `axolotl`, `llm-studio`.

### `swm setup install <framework> [id]`

Install a framework on a running instance.

```bash
swm setup install comfyui runpod:abc123
swm setup install axolotl runpod:abc123
swm setup install llm-studio runpod:abc123
```

### `swm setup start <framework> [id]`

Start a framework in the background.

| Option | Description |
|--------|-------------|
| `-p, --port INTEGER` | Override the default listen port |

### `swm setup stop <framework> [id]`

Stop a running framework.

### `swm setup storage [id]`

Install s5cmd and verify storage connectivity on an instance. Reads S3-compatible credentials from swm config and passes them as env vars per command — no files are written to the pod.

| Option | Description |
|--------|-------------|
| `-p, --provider [b2\|gcs\|s3\|all]` | Which storage backend to configure (default: `all` configured) |

### `swm setup workspace [id]`

Attach an object-storage workspace to an existing pod. Performs the full bootstrap: installs s5cmd, configures storage, pulls (or initializes) the workspace, persists the pod ↔ workspace ↔ bucket mapping, starts the auto-sync daemon.

| Option | Description |
|--------|-------------|
| `-n, --name TEXT` | Workspace name (default: pod name) |
| `-b, --bucket TEXT` | Bucket spec `provider:bucket` (default: configured) |
| `--force` | Overwrite existing workspace association |

Use when:

- You created a pod with `--no-storage` and want to add a workspace later
- `swm pod create`'s SSH probe timed out and bootstrap was skipped
- You want to reattach to a different workspace (`--force`)

## `swm costs`

Track GPU spending. Cost data lives in `~/.config/swm/costs.db` (SQLite, managed automatically).

### `swm costs summary`

Spending breakdown grouped by provider and GPU type.

| Option | Description |
|--------|-------------|
| `-t, --period [today\|week\|month\|all]` | Time window (default: `month`) |
| `-p, --provider TEXT` | Filter to one provider |

### `swm costs log`

Detailed session log — every pod run.

| Option | Description |
|--------|-------------|
| `-n, --limit INTEGER` | Number of sessions to show |
| `-p, --provider TEXT` | Filter to one provider |

### `swm costs live`

Show running cost of all active pods in real time.

### `swm costs reconcile`

Query provider billing APIs to show card charges, GPU usage breakdown, and compare with local tracking.

| Option | Description |
|--------|-------------|
| `-p, --provider [runpod\|vastai]` | Reconcile one provider only |

### `swm costs budget set <amount>`

Set a spending budget with advisory alerts at 80% and 100%.

| Option | Description |
|--------|-------------|
| `-s, --scope TEXT` | Budget scope: `global`, `provider:<slug>`, or `pod:<id>` |
| `-t, --period [daily\|weekly\|monthly\|total]` | Budget period (default: `monthly`) |

```bash
swm costs budget set 50                            # $50/month global
swm costs budget set 100 --scope provider:runpod
swm costs budget set 10 --period daily
```

### `swm costs budget show`

List active budgets with current spend and progress.

### `swm costs budget remove <scope>`

Remove a budget.

| Option | Description |
|--------|-------------|
| `-t, --period [daily\|weekly\|monthly\|total]` | Period to remove (default: `monthly`) |

## `swm guard`

Lifecycle automation. Monitors SSH sessions, GPU utilization, filesystem writes, transfers, and active processes; reminds, stops, or terminates idle pods according to policy.

### `swm guard defaults`

Show or update the global default policy applied to new pods. Run without options to display.

| Option | Description |
|--------|-------------|
| `--mode [manual\|remind\|auto-stop\|auto-down]` | Default mode |
| `--idle-timeout INTEGER` | Default idle timeout in minutes |
| `--poll-interval INTEGER` | Default on-pod watcher poll interval in seconds |

### `swm guard set [id]`

Configure the policy for a single pod.

| Option | Description |
|--------|-------------|
| `--mode [manual\|remind\|auto-stop\|auto-down]` | (required) |
| `--idle-timeout INTEGER` | Idle timeout in minutes |
| `--poll-interval INTEGER` | On-pod watcher poll interval in seconds |

### `swm guard disable [id]`

Remove the per-pod policy. The pod falls back to defaults if defaults are enabled, otherwise the guard is fully off.

### `swm guard list`

List guarded pods with policy, idle time, watcher status, and recent activity.

### `swm guard run [ids...]`

Run a guard cycle manually. Without IDs, evaluates every guarded pod.

| Option | Description |
|--------|-------------|
| `--once` | Run a single cycle and exit (default: loop) |
| `--interval INTEGER` | Loop interval in seconds |

### `swm guard stop-daemon`

Stop the local background guard daemon.

```bash
swm guard defaults --mode auto-down --idle-timeout 60
swm guard set runpod:abc123 --mode auto-down --idle-timeout 30
swm guard list
swm guard run --once
```

## `swm models`

Search HuggingFace Hub, download models to pods, and hot-swap the active vLLM model. Auto-detects engine from model name:

- `org/model-name` → HuggingFace download (for vLLM)
- `model:tag` → Ollama pull

### `swm models search <query>`

| Option | Description |
|--------|-------------|
| `--sort [downloads\|likes\|trending]` | Sort order (default: `downloads`) |
| `-n, --limit INTEGER` | Max results |
| `--all-types` | Include non-LLM models (image, audio, etc.) |

```bash
swm models search "qwen3 instruct"
swm models search "llama 4" --sort likes
swm models search "stable diffusion" --all-types
```

### `swm models info <model>`

Detailed metadata for a single HuggingFace model.

| Option | Description |
|--------|-------------|
| `--token TEXT` | HuggingFace token for gated models |

### `swm models pull [id] <model>`

Download a model to the pod's `/workspace/models` directory (HF) or to ollama's store.

| Option | Description |
|--------|-------------|
| `--token TEXT` | HuggingFace token (overrides `swm config set hf_token`) |

```bash
swm models pull runpod:abc123 Qwen/Qwen3-8B
swm models pull runpod:abc123 deepseek-r1:14b
swm models pull runpod:abc123 meta-llama/Llama-4-Scout --token hf_xxx
```

### `swm models set [id] <model>`

Activate a HuggingFace model for vLLM. Writes the launch config and restarts vLLM by default.

| Option | Description |
|--------|-------------|
| `--restart` / `--no-restart` | Restart vLLM after setting (default: yes) |

### `swm models list [id]`

List models already downloaded on the pod (both vLLM and Ollama).

### `swm models remove [id] <model>`

Delete a downloaded model from the pod.

| Option | Description |
|--------|-------------|
| `-y, --yes` | Skip confirmation |

## `swm storage`

Manage cloud storage buckets directly. Supports GCS, Backblaze B2, and S3 via S3-compatible APIs.

### `swm storage list`

List buckets across configured storage providers.

| Option | Description |
|--------|-------------|
| `-p, --provider [gcs\|b2\|s3]` | Filter to one provider |

### `swm storage create <name>`

Create a new storage bucket.

| Option | Description |
|--------|-------------|
| `-p, --provider [gcs\|b2\|s3]` | (required) |
| `-l, --location TEXT` | Bucket location/region |
| `-c, --storage-class TEXT` | Storage class (`STANDARD`, `NEARLINE`, `allPrivate`, …) |

### `swm storage ls [path]`

List contents of a bucket.

| Option | Description |
|--------|-------------|
| `-b, --bucket TEXT` | Bucket (default from config); use `provider:bucket` for explicit |

### `swm storage upload <local> <remote>`

Upload a file to a bucket.

| Option | Description |
|--------|-------------|
| `-b, --bucket TEXT` | Target bucket (`provider:bucket`) |

### `swm storage download <remote> <local>`

Download a file from a bucket.

| Option | Description |
|--------|-------------|
| `-b, --bucket TEXT` | Source bucket (`provider:bucket`) |

### `swm storage rm <prefix>`

Delete every object under a prefix (directory). Uses the S3 batch delete API — ~1000× faster than deleting one at a time.

| Option | Description |
|--------|-------------|
| `-b, --bucket TEXT` | Bucket (`provider:bucket`) |
| `--dry-run` | Count objects without deleting |
| `-y, --yes` | Skip confirmation |

## `swm images`

List Docker images available to `swm pod create --image` for a given provider, parsed live from the provider's image registry.

### `swm images list`

| Option | Description |
|--------|-------------|
| `-p, --provider` | Provider (default: `runpod`) |
| `--cuda TEXT` | Filter to a CUDA `major.minor` (e.g. `12.8`) |
| `--refresh` | Bypass the local cache |
| `-n, --limit INTEGER` | Max rows (default: `20`) |
| `--all` | Show every image |

```bash
swm images list                              # all RunPod pytorch images
swm images list -p runpod --cuda 12.8        # CUDA 12.8 only
swm images list -p runpod --cuda 12.8 -n 5   # top 5
```

`swm pod create --cuda 12.8` resolves to the newest image returned here.

## `swm pricing`

Curated comparison of GPU pricing and workload cost estimates. Currently covers H200 and B200 across all major providers.

### `swm pricing compare`

Side-by-side per-GPU/hr pricing table including security certifications, stop/resume capability, and multi-GPU node options.

| Option | Description |
|--------|-------------|
| `--gpu [h200\|b200]` | Filter by GPU type |
| `--single-gpu` | Only show single-GPU offerings |

### `swm pricing estimate`

Estimate monthly cost for a workload (includes idle storage cost where applicable).

| Option | Description |
|--------|-------------|
| `--gpu [h200\|b200]` | GPU type |
| `--hours FLOAT` | Hours per week |
| `--storage FLOAT` | Model storage in GB |
| `--provider TEXT` | Filter to one provider |
| `--single-gpu` | Only single-GPU offerings |
| `--tier [on_demand\|spot\|reserved]` | Pricing tier |

### `swm pricing specs`

GPU hardware specs side-by-side (VRAM, bandwidth, BF16/FP8 TFLOPS, estimated 720p generation time).

| Option | Description |
|--------|-------------|
| `--gpu [h200\|b200]` | Filter to one GPU |

## `swm config`

Manage `~/.config/swm/config.toml`.

| Command | Description |
|---------|-------------|
| `swm config set <key> <value>` | Set a config value |
| `swm config get <key>` | Read a config value |
| `swm config list` | Show all config (sensitive values masked) |
| `swm config delete <key>` | Remove a key |
| `swm config path` | Show the config file location |

Keys use dot notation: `runpod.api_key`, `aws.region`, `b2.bucket`, `storage.default`, etc. See [configuration.md](./configuration.md) for the full key reference.

## Remote access

### `swm ssh [id]`

Open an interactive SSH session.

```bash
swm ssh runpod:abc123
swm ssh                       # uses active pod
```

### `swm run [id] <command>`

Run a command on a remote instance and stream the output.

| Option | Description |
|--------|-------------|
| `-q, --quiet` | Suppress real-time output |

```bash
swm run runpod:abc123 nvidia-smi       # explicit pod
swm run nvidia-smi                     # uses active pod
swm run runpod:abc123 -- ls -la /ws    # use -- to escape option parsing
```

### `swm upload [id] <local> [remote]`

Upload a file or directory to a running instance. Remote path defaults to `/workspace/`. Relative remote paths are placed under `/workspace/`.

| Option | Description |
|--------|-------------|
| `-r, --recursive` | Upload a directory recursively |

```bash
swm upload runpod:abc123 ./model.safetensors
swm upload runpod:abc123 ./model.safetensors models/
swm upload runpod:abc123 ./loras/ models/loras -r
```

### `swm download [id] <remote>`

Download a file or directory. Directories use tar-over-SSH (compressed stream) which is significantly faster than `scp -r`. Relative remote paths resolve under `/workspace/`.

| Option | Description |
|--------|-------------|
| `-d, --dir PATH` | Local directory to save into (default: current dir) |

```bash
swm download runpod:abc123 output.mp4
swm download runpod:abc123 output.mp4 -d ~/Downloads
swm download runpod:abc123 ComfyUI/output/ -d ./results
```

## Provider slugs

For `--provider` flags and `provider:id` ids:

| Slug | Provider |
|------|----------|
| `runpod` | RunPod |
| `vastai` | Vast.ai |
| `lambda` | Lambda Labs |
| `vultr` | Vultr |
| `tensordock` | TensorDock |
| `fluidstack` | FluidStack |
| `aws` | Amazon Web Services (EC2) |
| `gcp` | Google Cloud Platform (Compute Engine) |
| `azure` | Microsoft Azure |
| `coreweave` | CoreWeave |
