# Storage setup

swm syncs your `/workspace` directory to cloud storage so files persist across pods and providers. All three backends use the S3-compatible API.

## Backblaze B2 (recommended)

Backblaze B2 is the cheapest option at $0.006/GB/month with free egress to most GPU providers.

### Setup

1. Create an application key at [secure.backblaze.com/app_keys.htm](https://secure.backblaze.com/app_keys.htm)
2. Configure swm:

```bash
swm config set b2.key_id <applicationKeyId>
swm config set b2.app_key <applicationKey>
swm config set b2.bucket <bucket-name>
swm config set storage.default b2:<bucket-name>
```

The S3 endpoint is auto-detected on first use (via the `b2` CLI if installed). If auto-detection fails, set it manually:

```bash
swm config set b2.s3_endpoint https://s3.us-west-004.backblazeb2.com
```

### Create a bucket

```bash
swm storage create my-workspace -p b2 -l us-west-004 -c allPrivate
```

## Amazon S3

Use explicit keys or the standard AWS credential chain (env vars, `~/.aws/credentials`, IAM roles).

**Option A — explicit keys:**

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

boto3 will discover credentials from `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` env vars, `~/.aws/credentials`, or IAM instance roles automatically.

**Custom endpoints** — Cloudflare R2, MinIO, etc:

```bash
swm config set s3.endpoint_url https://<accountid>.r2.cloudflarestorage.com
swm config set s3.bucket my-bucket
swm config set storage.default s3:my-bucket
```

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

2. Create HMAC keys (the secret is displayed only once):

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

> The HMAC secret is shown only at creation time. Store it immediately. If lost, create a new key pair.

### Create a bucket

```bash
swm storage create my-workspace -p gcs -l us-central1 -c STANDARD
```

## Managing storage

```bash
swm storage list                                  # all buckets across providers
swm storage ls                                    # default bucket root
swm storage ls models/                            # a subdirectory
swm storage ls -b gcs:swm-models                  # explicit provider:bucket
swm storage upload model.safetensors models/      # single-file upload
swm storage download models/model.safetensors ./  # single-file download
swm storage rm workspace2/SwarmUI/                # batch-delete a prefix
swm storage rm workspace2/SwarmUI/ --dry-run      # count only, no delete
```

`swm storage rm` uses the S3 batch delete API — it removes 1000 objects per request, ~1000× faster than deleting one at a time. Always `--dry-run` first if unsure of the prefix.

## How workspace sync works

When you run `swm pod create`, swm automatically:

1. Installs **s5cmd** on the pod (a high-performance S3 transfer tool)
2. Configures storage credentials (passed transiently via SSH, never written to disk)
3. Pulls your workspace from the bucket to `/workspace` on the pod
4. Starts the inotify watcher
5. Starts the auto-sync daemon (default: every 60s)

When you run `swm pod down`, swm:

1. Pushes `/workspace` back to the bucket (only new / changed files)
2. Terminates the pod

### Sync behavior

- **Pull** uses `s5cmd cp --no-clobber` — skips files that already exist on the pod
- **Push** uses a three-tier strategy: inotify watcher → timestamp-based delta → full `s5cmd cp --if-size-differ`
- **Non-destructive by default** — `sync push`, `sync pull`, and `pod down` never delete from your bucket
- **Opt-in deletion** — pass `--delete` to `swm sync push` to mirror local deletions; swm refuses unless an active watcher is recording changes
- **Exclude patterns** — `swm sync push <id> -x "*.tmp" -x "__pycache__"`

### Tar mode for many small files

For workspaces with 100k+ small files (e.g. ComfyUI custom_nodes / venvs), normal sync makes hundreds of thousands of S3 API calls:

```bash
swm sync push runpod:abc123 --tar
swm sync pull runpod:abc123 --tar     # later, from a new pod
```

`--tar` packs the entire workspace into a single compressed tarball — turning 600k API calls into one. Trade-off: any change requires re-uploading the whole tarball.

### Continuous auto-sync

`swm sync auto <id>` runs a background daemon that tails the watcher log and pushes changes every interval (default 60s). It's started for you by `swm pod create`; manage it with:

```bash
swm sync auto runpod:abc123 --status   # daemon state + recent log
swm sync auto runpod:abc123 -i 30      # 30s interval
swm sync auto runpod:abc123 --stop     # stop the daemon
```

The daemon refuses to start unless a prior `swm sync pull` or `swm sync push` succeeded for this pod — without that signal, a stray local deletion would propagate to storage and erase the remote copy. `--force` bypasses this check (dangerous; reserved for advanced recovery scenarios).

### Attaching a workspace later

If you created a pod with `--no-storage`, or `swm pod create`'s SSH probe timed out before bootstrap finished, attach storage in one shot:

```bash
swm setup workspace runpod:abc123                   # workspace name = pod name
swm setup workspace runpod:abc123 -n my-ws          # custom name
swm setup workspace runpod:abc123 -b b2:my-bucket   # explicit bucket
swm setup workspace runpod:abc123 --force           # reattach (replaces existing)
```

This installs s5cmd, configures storage, pulls (or initializes) the workspace, persists the mapping in config, and starts auto-sync.

### Workspace names

Each pod is associated with a workspace name (e.g. `workspace`, `train-run-2`). If you don't specify one, swm picks the next available name. To restore a previous workspace on a new pod:

```bash
swm pod create -p runpod -g h200 -n new-session -w my-workspace
```

### Bucket override per command

Any `swm sync` command accepts `-b provider:bucket` to override the configured default for that invocation:

```bash
swm sync push runpod:abc123 -b gcs:backup-bucket
swm sync pull runpod:abc123 -b s3:disaster-recovery
```
