# Configuration Reference

Config lives at `~/.config/swm/config.toml`. All values are set via `swm config set <key> <value>`.

## Provider Credentials

| Key | Description |
|-----|-------------|
| `runpod.api_key` | RunPod API key |
| `vastai.api_key` | Vast.ai API key |
| `lambda.api_key` | Lambda Labs API key |
| `aws.region` | AWS region (default: `us-east-1`) |
| `aws.ami` | Custom AMI ID for EC2 instances |
| `aws.key_name` | EC2 key pair name |
| `aws.subnet_id` | VPC subnet ID |
| `aws.security_group` | Security group ID |
| `gcp.project` | GCP project ID |
| `gcp.zone` | GCP zone (e.g., `us-central1-a`) |
| `coreweave.kubeconfig` | Path to CoreWeave kubeconfig |
| `coreweave.namespace` | Kubernetes namespace |

## Storage (S3-Compatible)

All storage providers use the S3 API via boto3. See [Storage Setup](storage.md) for credential setup.

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

## SSH

| Key | Description |
|-----|-------------|
| `ssh.key_path` | Path to SSH private key (default: auto-detect from `~/.ssh/`) |
| `<provider>.ssh_key` | Per-provider SSH key path override |
| `<provider>.ssh_user` | Per-provider SSH user override |

## Pod Metadata (auto-managed)

These are set automatically by `swm pod create` and cleaned up by `swm pod down`:

| Key | Description |
|-----|-------------|
| `pods.<id>.provider` | Provider slug |
| `pods.<id>.name` | Pod name |
| `pods.<id>.workspace` | Workspace name in bucket |
| `pods.<id>.storage` | Storage spec (`b2:bucket-name`) |

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
