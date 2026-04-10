# Configuration

All configuration lives at `~/.config/swm/config.toml`. Manage it with `swm config set/get/list/delete`.

## GPU Provider Credentials

Configure one or more providers. swm queries all configured providers when you run `swm gpus`.

### RunPod

```bash
swm config set runpod.api_key <your-api-key>
```

Get your API key at [runpod.io/console/user/settings](https://www.runpod.io/console/user/settings).

### Vast.ai

```bash
swm config set vastai.api_key <your-api-key>
```

Get your API key at [cloud.vast.ai/account](https://cloud.vast.ai/account/).

### Lambda Labs

```bash
swm config set lambda.api_key <your-api-key>
```

Get your API key at [cloud.lambdalabs.com/api-keys](https://cloud.lambdalabs.com/api-keys).

### Vultr

```bash
swm config set vultr.api_key <your-api-key>
```

Get your API key at [my.vultr.com/settings/#settingsapi](https://my.vultr.com/settings/#settingsapi).

### TensorDock

```bash
swm config set tensordock.api_token <your-api-token>
```

Get your token from the TensorDock dashboard.

### FluidStack

```bash
swm config set fluidstack.api_key <your-api-key>
```

Optional additional configuration:

```bash
swm config set fluidstack.project_id <project-id>
swm config set fluidstack.region_url <region-url>  # auto-discovered if omitted
```

### AWS (EC2)

AWS uses standard boto3 credentials (environment variables, `~/.aws/credentials`, or IAM roles). No API key is needed in swm config.

```bash
swm config set aws.region us-east-1              # default region
swm config set aws.ami ami-xxxxx                  # custom AMI (optional)
swm config set aws.key_name my-key                # EC2 key pair name (optional)
swm config set aws.subnet_id subnet-xxxxx         # VPC subnet (optional)
swm config set aws.security_group sg-xxxxx        # security group (optional)
```

### GCP (Compute Engine)

GCP uses `gcloud` CLI authentication. Ensure `gcloud auth login` is configured.

```bash
swm config set gcp.project my-project-id          # required
swm config set gcp.zone us-central1-a             # default zone (optional)
```

### Azure

Azure uses service principal authentication.

```bash
swm config set azure.tenant_id <tenant-id>
swm config set azure.client_id <client-id>
swm config set azure.client_secret <client-secret>
swm config set azure.subscription_id <subscription-id>
swm config set azure.resource_group swm-rg        # optional (default: swm-rg)
swm config set azure.location eastus              # optional (default: eastus)
```

### CoreWeave

CoreWeave uses a Kubernetes kubeconfig file.

```bash
swm config set coreweave.kubeconfig /path/to/kubeconfig
swm config set coreweave.namespace default        # optional
```

## Storage Credentials

swm uses S3-compatible APIs for all storage backends. See [Storage Setup](storage.md) for detailed instructions.

### Backblaze B2

```bash
swm config set b2.key_id <applicationKeyId>
swm config set b2.app_key <applicationKey>
swm config set b2.bucket <bucket-name>
swm config set storage.default b2:<bucket-name>
```

### Amazon S3

```bash
swm config set s3.access_key <AWS_ACCESS_KEY_ID>       # optional if using AWS chain
swm config set s3.secret_key <AWS_SECRET_ACCESS_KEY>    # optional if using AWS chain
swm config set s3.bucket <bucket-name>
```

### Google Cloud Storage

```bash
swm config set gcs.hmac_access <access-id>
swm config set gcs.hmac_secret <secret>
swm config set gcp.bucket <bucket-name>
```

## SSH

```bash
swm config set ssh.key_path ~/.ssh/id_ed25519     # custom SSH key path
```

Per-provider overrides:

```bash
swm config set runpod.ssh_key ~/.ssh/runpod_key
swm config set runpod.ssh_user root
```

## Cost Tracking

Cost data is stored in a SQLite database at `~/.config/swm/costs.db`. This is managed automatically — no configuration needed.

Budget alerts are advisory and never block operations. Set them with:

```bash
swm costs budget set <amount> [--scope <scope>] [--period <period>]
```

## Full Config Reference

| Key | Required | Description |
|-----|----------|-------------|
| **RunPod** | | |
| `runpod.api_key` | Yes | RunPod API key |
| **Vast.ai** | | |
| `vastai.api_key` | Yes | Vast.ai API key |
| **Lambda Labs** | | |
| `lambda.api_key` | Yes | Lambda Labs API key |
| **Vultr** | | |
| `vultr.api_key` | Yes | Vultr API key |
| **TensorDock** | | |
| `tensordock.api_token` | Yes | TensorDock API token |
| **FluidStack** | | |
| `fluidstack.api_key` | Yes | FluidStack API key |
| `fluidstack.project_id` | No | Project ID for infra API |
| `fluidstack.region_url` | No | Region URL (auto-discovered) |
| **AWS** | | |
| `aws.region` | No | AWS region (default: us-east-1) |
| `aws.ami` | No | Custom AMI ID |
| `aws.key_name` | No | EC2 key pair name |
| `aws.subnet_id` | No | VPC subnet ID |
| `aws.security_group` | No | Security group ID |
| **GCP** | | |
| `gcp.project` | Yes | GCP project ID |
| `gcp.zone` | No | GCP zone (default: us-central1-a) |
| **Azure** | | |
| `azure.tenant_id` | Yes | Azure AD tenant ID |
| `azure.client_id` | Yes | Service principal client ID |
| `azure.client_secret` | Yes | Service principal secret |
| `azure.subscription_id` | Yes | Azure subscription ID |
| `azure.resource_group` | No | Resource group (default: swm-rg) |
| `azure.location` | No | Azure region (default: eastus) |
| **CoreWeave** | | |
| `coreweave.kubeconfig` | No | Path to kubeconfig file |
| `coreweave.namespace` | No | Kubernetes namespace (default: default) |
| **Storage** | | |
| `b2.key_id` | Yes* | B2 application key ID |
| `b2.app_key` | Yes* | B2 application key |
| `b2.bucket` | Yes* | B2 bucket name |
| `b2.s3_endpoint` | No | B2 S3 endpoint (auto-detected) |
| `gcs.hmac_access` | Yes* | GCS HMAC access ID |
| `gcs.hmac_secret` | Yes* | GCS HMAC secret |
| `gcp.bucket` | Yes* | GCS bucket name |
| `s3.access_key` | No | AWS access key |
| `s3.secret_key` | No | AWS secret key |
| `s3.bucket` | Yes* | S3 bucket name |
| `storage.default` | Yes | Default storage (e.g., b2:my-bucket) |
| **SSH** | | |
| `ssh.key_path` | No | SSH private key path |

\* Required only if using that storage backend.
