# Storage Setup

All three storage providers use the S3-compatible API via `boto3`. Each provider needs its own credentials.

## Backblaze B2

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

## Google Cloud Storage

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

## Amazon S3

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
