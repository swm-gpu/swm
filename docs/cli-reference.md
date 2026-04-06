# CLI Reference

## Global

| Command | Description |
|---------|-------------|
| `swm --version` | Show version |
| `swm gpus [-g gpu] [-c count] [--max-price N]` | **Search live GPU availability & pricing across all providers** |
| `swm ssh <instance_id>` | Open interactive SSH session |
| `swm run <instance_id> <command>` | Execute a command remotely |
| `swm upload <instance_id> <local> [remote]` | SCP file/dir to pod |
| `swm download <instance_id> <remote> [-d dir]` | SCP file/dir from pod |

## `swm gpus`

Search live GPU availability and pricing across all providers. Paginated (default 20 rows).

| Option | Description |
|--------|-------------|
| `-g, --gpu TEXT` | Filter by GPU name (free text, e.g. h200, a100, rtx4090) |
| `-c, --count N` | GPU count (e.g. 4 for 4x configs) |
| `--max-price N` | Max on-demand $/hr per GPU |
| `-p, --provider TEXT` | Filter to one provider |
| `--secure` | Only show secure-cloud providers |
| `--sort [price\|vram\|provider]` | Sort order (default: price) |
| `-n, --limit N` | Max rows to show (default: 20) |
| `--all` | Show all results |

## `swm config`

| Command | Description |
|---------|-------------|
| `swm config set <key> <value>` | Set a config value |
| `swm config get <key>` | Read a config value |
| `swm config list` | Show all config |
| `swm config delete <key>` | Remove a key |
| `swm config path` | Show config file location |

## `swm pod`

| Command | Description |
|---------|-------------|
| `swm pod create -p <provider> -g <gpu> -n <name>` | Provision + bootstrap |
| `swm pod list` | List instances across providers |
| `swm pod status <id>` | Detailed instance info |
| `swm pod start <id>` | Resume a stopped instance |
| `swm pod stop <id>` | Stop (preserves volume) |
| `swm pod down <id>` | Push workspace + terminate |
| `swm pod terminate <id>` | Destroy instance + volume |

## `swm sync`

| Command | Description |
|---------|-------------|
| `swm sync pull <id> [path]` | Pull workspace/subdir from storage to pod |
| `swm sync push <id> [path]` | Push workspace/subdir from pod to storage |
| `swm sync status <id>` | Show storage sync status on pod |

## `swm setup`

| Command | Description |
|---------|-------------|
| `swm setup list` | List available frameworks |
| `swm setup install <framework> <id>` | Install a framework (comfyui, swarmui, axolotl, llm-studio) |
| `swm setup start <framework> <id>` | Start a framework in the background |
| `swm setup stop <framework> <id>` | Stop a running framework |
| `swm setup storage <id>` | Install s5cmd + verify S3 connection |

## `swm storage`

| Command | Description |
|---------|-------------|
| `swm storage list` | List buckets |
| `swm storage create <name> -p <provider>` | Create a bucket |
| `swm storage ls [path]` | List bucket contents |
| `swm storage upload <local> <remote>` | Upload to bucket |
| `swm storage download <remote> <local>` | Download from bucket |

## `swm pricing`

| Command | Description |
|---------|-------------|
| `swm pricing compare` | Side-by-side GPU pricing table (static reference) |
| `swm pricing estimate --gpu h200 --hours 3` | Monthly cost estimate |
| `swm pricing specs` | GPU hardware specs comparison |
