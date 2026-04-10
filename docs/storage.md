# Storage Setup

swm syncs your `/workspace` directory to cloud storage so your files persist across pods and providers. All three backends use the S3-compatible API.

## Backblaze B2 (Recommended)

Backblaze B2 is the most cost-effective option at $0.006/GB/month with free egress to most compute providers.

### Setup

1. Create an application key at [secure.backblaze.com/app_keys.htm](https://secure.backblaze.com/app_keys.htm)
2. Configure swm:

```bash
swm config set b2.key_id <applicationKeyId>
swm config set b2.app_key <applicationKey>
swm config set b2.bucket <bucket-name>
swm config set storage.default b2:<bucket-name>
```

The S3 endpoint is auto-detected on first use. If auto-detection fails, set it manually:

```bash
swm config set b2.s3_endpoint https://s3.us-west-004.backblazeb2.com
```

### Create a bucket

```bash
swm storage create my-workspace -p b2 -l us-west-004 -c allPrivate
```

## Amazon S3

### Setup

You can use explicit keys or the standard AWS credential chain (environment variables, `~/.aws/credentials`, IAM roles).

**Option A — Explicit keys:**

```bash
swm config set s3.access_key <AWS_ACCESS_KEY_ID>
swm config set s3.secret_key <AWS_SECRET_ACCESS_KEY>
swm config set s3.bucket <bucket-name>
swm config set storage.default s3:<bucket-name>
```

**Option B — AWS credential chain:**

```bash
swm config set s3.bucket <bucket-name>
swm config set storage.default s3:<bucket-name>
```

boto3 will find credentials from `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` environment variables, `~/.aws/credentials`, or IAM instance roles automatically.

### Create a bucket

```bash
swm storage create my-workspace -p s3 -l us-east-1
```

## Google Cloud Storage

GCS requires HMAC keys for S3 compatibility.

### Setup

1. Find your service account:

```bash
gcloud iam service-accounts list --project=<project-id>
```

2. Create HMAC keys (the secret is shown only once):

```bash
gcloud storage hmac create <service-account-email> --project=<project-id>
```

3. Configure swm:

```bash
swm config set gcs.hmac_access <access-id>
swm config set gcs.hmac_secret <secret>
swm config set gcp.bucket <bucket-name>
swm config set storage.default gcs:<bucket-name>
```

> **Note:** The HMAC secret is only displayed at creation time. Store it immediately. If lost, create a new key pair.

### Create a bucket

```bash
swm storage create my-workspace -p gcs -l us-central1 -c STANDARD
```

## Managing Storage

```bash
swm storage list                    # list all buckets
swm storage ls                      # list contents of default bucket
swm storage ls models/              # list a subdirectory
swm storage upload model.safetensors models/
swm storage download models/model.safetensors ./
```

## How Workspace Sync Works

When you run `swm pod create`, swm automatically:

1. Installs **s5cmd** on the pod (a high-performance S3 transfer tool)
2. Configures storage credentials (passed transiently via SSH, never written to disk)
3. Pulls your workspace from the bucket to `/workspace` on the pod

When you run `swm pod down`, swm:

1. Pushes `/workspace` back to the bucket (only new/changed files)
2. Terminates the pod

### Sync behavior

- **Pull** uses `s5cmd cp --no-clobber` — skips files that already exist on the pod
- **Push** uses a three-tier strategy: inotify watcher, timestamp-based delta, or full `s5cmd cp --if-size-differ`
- Both directions are **non-destructive** — files are never deleted from your bucket
- Exclude patterns are supported: `swm sync push <id> -x "*.tmp" -x "__pycache__"`

### Workspace names

Each pod is associated with a workspace name (e.g., `workspace`, `train-run-2`). If you don't specify one, swm picks the next available name. To restore a previous workspace on a new pod:

```bash
swm pod create -p runpod -g h200 -n new-session -w my-workspace
```
