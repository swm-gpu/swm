# Configuration

All configuration lives at `~/.config/swm/config.toml`. Manage it with:

```bash
swm config set <key> <value>     # write
swm config get <key>              # read
swm config list                   # dump (sensitive values masked)
swm config delete <key>           # remove
swm config path                   # show file location
```

Keys use dot notation (`runpod.api_key`, `b2.bucket`, `aws.region`, …) and are stored as a single TOML file.

## Environment variables

| Variable | Purpose |
|----------|---------|
| `SWM_POD` | Override the active pod id for one invocation (`SWM_POD=runpod:abc123 swm ssh`) |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | Picked up by boto3 if `s3.access_key` / `s3.secret_key` aren't set |
| `HF_TOKEN` / `HUGGING_FACE_HUB_TOKEN` | Used by `swm models pull` when neither `--token` nor `swm config set hf.api_key` is set |
| `CIVITAI_API_KEY` | Used by `swm models pull` / `swm models info` for Civitai when `civitai.api_key` is not set |

## GPU provider credentials

Configure one or more providers. `swm gpus` queries every configured provider in parallel.

### RunPod

```bash
swm config set runpod.api_key <key>
```

Get your key at [runpod.io/console/user/settings](https://www.runpod.io/console/user/settings).

### Vast.ai

```bash
swm config set vastai.api_key <key>
```

Get your key at [cloud.vast.ai/account](https://cloud.vast.ai/account/).

### Lambda Labs

```bash
swm config set lambda.api_key <key>
```

Get your key at [cloud.lambdalabs.com/api-keys](https://cloud.lambdalabs.com/api-keys).

### Vultr

```bash
swm config set vultr.api_key <key>
```

Get your key at [my.vultr.com/settings/#settingsapi](https://my.vultr.com/settings/#settingsapi).

### TensorDock

```bash
swm config set tensordock.api_token <token>
```

### FluidStack

```bash
swm config set fluidstack.api_key <key>
swm config set fluidstack.project_id <project>   # optional
swm config set fluidstack.region_url <url>       # auto-discovered if omitted
```

### AWS (EC2)

AWS uses standard boto3 credentials — environment variables, `~/.aws/credentials`, or IAM roles. No API key is needed in swm config; the configurable bits are region and EC2 specifics:

```bash
swm config set aws.region us-east-1
swm config set aws.ami ami-xxxxx              # custom AMI (optional)
swm config set aws.key_name my-key            # EC2 key pair name
swm config set aws.subnet_id subnet-xxxxx     # VPC subnet
swm config set aws.security_group sg-xxxxx    # security group
swm config set aws.ssh_key ~/.ssh/aws_key     # per-provider SSH override
```

### GCP (Compute Engine)

GCP uses `gcloud` CLI authentication. Run `gcloud auth login` once.

```bash
swm config set gcp.project my-project-id
swm config set gcp.zone us-central1-a         # default zone (optional)
```

### Azure

Azure uses service principal authentication.

```bash
swm config set azure.tenant_id <tenant>
swm config set azure.client_id <client>
swm config set azure.client_secret <secret>
swm config set azure.subscription_id <sub>
swm config set azure.resource_group swm-rg    # default: swm-rg
swm config set azure.location eastus          # default: eastus
```

### CoreWeave

CoreWeave talks to a Kubernetes cluster. Install the `kubernetes` Python SDK with `pip install 'swm-gpu[coreweave]'`.

```bash
swm config set coreweave.kubeconfig /path/to/kubeconfig
swm config set coreweave.namespace default    # default: default
```

## Storage credentials

All storage backends use the S3-compatible API. See [storage.md](./storage.md) for full walk-throughs.

### Backblaze B2

```bash
swm config set b2.key_id <applicationKeyId>
swm config set b2.app_key <applicationKey>
swm config set b2.bucket <bucket-name>
swm config set storage.default b2:<bucket-name>
```

The S3 endpoint is auto-detected on first use. Set it manually if needed:

```bash
swm config set b2.s3_endpoint https://s3.us-west-004.backblazeb2.com
```

### Amazon S3

You can use explicit keys or the standard AWS credential chain.

```bash
swm config set s3.access_key <AWS_ACCESS_KEY_ID>      # optional
swm config set s3.secret_key <AWS_SECRET_ACCESS_KEY>  # optional
swm config set s3.bucket <bucket-name>
swm config set s3.endpoint_url https://...            # custom endpoint (R2, MinIO)
swm config set storage.default s3:<bucket-name>
```

### Google Cloud Storage

GCS uses HMAC keys for S3 compatibility.

```bash
swm config set gcs.hmac_access <access-id>
swm config set gcs.hmac_secret <secret>
swm config set gcp.bucket <bucket-name>
swm config set storage.default gcs:<bucket-name>
```

## SSH

```bash
swm config set ssh.key_path ~/.ssh/id_ed25519     # default SSH key
```

Per-provider overrides (e.g. distinct keys for AWS vs RunPod):

```bash
swm config set runpod.ssh_key ~/.ssh/runpod_key
swm config set aws.ssh_key ~/.ssh/aws_key
```

## Lifecycle guard defaults

Applied to every new pod created with `swm pod create` (unless overridden by `--lifecycle` / `--idle-timeout`):

```bash
swm config set guard.defaults.mode auto-stop            # manual|remind|auto-stop|auto-down
swm config set guard.defaults.idle_timeout_minutes 60
swm config set guard.defaults.poll_interval_seconds 300
```

Or via the dedicated command:

```bash
swm guard defaults --mode auto-down --idle-timeout 60 --poll-interval 300
```

## Cost tracking

Cost data lives in a SQLite database at `~/.config/swm/costs.db` — managed automatically. Budget alerts are advisory and never block operations:

```bash
swm costs budget set 50                            # $50/month global
swm costs budget set 100 --scope provider:runpod   # per-provider
swm costs budget set 10 --period daily             # daily cap
```

## HuggingFace token

Used by `swm models pull` / `swm models info` for gated models:

```bash
swm config set hf_token hf_xxxxxxxxxxxxxxxxxxxxxx
```

`HF_TOKEN` / `HUGGING_FACE_HUB_TOKEN` env vars are also honored.

## Active pod

`swm use <pod_id>` writes the `active_pod` key; subsequent commands that take `[INSTANCE_ID]` will fall back to it if no id is passed and `$SWM_POD` isn't set.

```bash
swm use runpod:abc123     # set
swm use --show            # read
swm use --clear           # delete
```

## Pod metadata

Set automatically by `swm pod create` and `swm setup workspace`; rarely edited by hand. Stored under `pods.<provider:id>.*`:

| Sub-key | Description |
|---------|-------------|
| `name` | Friendly name (`my-session`) |
| `provider` | Provider slug (`runpod`) |
| `workspace` | Workspace name in storage |
| `storage` | Storage spec (`b2:my-bucket`) |
| `guard.mode` | Per-pod lifecycle policy |
| `guard.idle_timeout_minutes` | Per-pod override |
| `guard.poll_interval_seconds` | Per-pod override |
| `guard.last_notice_ts` | Last reminder timestamp (managed) |

## Full config key reference

| Key | Required | Description |
|-----|----------|-------------|
| **Providers** | | |
| `runpod.api_key` | Yes | RunPod API key |
| `vastai.api_key` | Yes | Vast.ai API key |
| `lambda.api_key` | Yes | Lambda Labs API key |
| `vultr.api_key` | Yes | Vultr API key |
| `tensordock.api_token` | Yes | TensorDock API token |
| `fluidstack.api_key` | Yes | FluidStack API key |
| `fluidstack.project_id` | No | Project id for the infra API |
| `fluidstack.region_url` | No | Region URL (auto-discovered) |
| `aws.region` | No | AWS region (default: `us-east-1`) |
| `aws.ami` | No | Custom AMI id |
| `aws.key_name` | No | EC2 key pair name |
| `aws.subnet_id` | No | VPC subnet id |
| `aws.security_group` | No | Security group id |
| `aws.ssh_key` | No | Per-provider SSH key |
| `gcp.project` | Yes | GCP project id |
| `gcp.zone` | No | GCP zone (default: `us-central1-a`) |
| `azure.tenant_id` | Yes | Azure AD tenant id |
| `azure.client_id` | Yes | Service principal client id |
| `azure.client_secret` | Yes | Service principal secret |
| `azure.subscription_id` | Yes | Azure subscription id |
| `azure.resource_group` | No | Default: `swm-rg` |
| `azure.location` | No | Default: `eastus` |
| `coreweave.kubeconfig` | Yes\* | Path to kubeconfig file |
| `coreweave.namespace` | No | Kubernetes namespace (default: `default`) |
| **Storage** | | |
| `b2.key_id` | Yes\* | B2 application key id |
| `b2.app_key` | Yes\* | B2 application key |
| `b2.bucket` | Yes\* | B2 bucket name |
| `b2.s3_endpoint` | No | B2 S3 endpoint (auto-detected) |
| `gcs.hmac_access` | Yes\* | GCS HMAC access id |
| `gcs.hmac_secret` | Yes\* | GCS HMAC secret |
| `gcp.bucket` | Yes\* | GCS bucket name |
| `s3.access_key` | No | AWS access key (falls back to boto3 chain) |
| `s3.secret_key` | No | AWS secret key |
| `s3.bucket` | Yes\* | S3 bucket name |
| `s3.endpoint_url` | No | Custom S3 endpoint (R2, MinIO, …) |
| `storage.default` | Yes | Default storage spec (`b2:my-bucket`) |
| **SSH** | | |
| `ssh.key_path` | No | Default SSH private key path |
| `<provider>.ssh_key` | No | Per-provider SSH key override |
| **Guard** | | |
| `guard.defaults.mode` | No | `manual` / `remind` / `auto-stop` / `auto-down` |
| `guard.defaults.idle_timeout_minutes` | No | Default idle window |
| `guard.defaults.poll_interval_seconds` | No | Default poll interval |
| **Misc** | | |
| `active_pod` | No | Set by `swm use` |
| `hf_token` | No | HuggingFace token |

\* Required only if using that storage backend / provider.
