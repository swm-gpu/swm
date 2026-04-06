# Extending swm

## Adding a New GPU Provider

To add support for a new provider (e.g., Nebius, Cudo Compute):

### 1. Create the provider module

Create `src/swm/providers/nebius.py`:

```python
from swm.providers.base import (
    CloudProvider, CreateConfig, GpuInfo, Instance, InstanceStatus,
)

class NebiusProvider(CloudProvider):
    @property
    def name(self) -> str:
        return "Nebius"

    @property
    def slug(self) -> str:
        return "nebius"

    def is_configured(self) -> bool:
        from swm import config as cfg
        return cfg.get("nebius.api_key") is not None

    def list_instances(self) -> list[Instance]: ...
    def create_instance(self, config: CreateConfig) -> Instance: ...
    def start_instance(self, instance_id: str) -> Instance: ...
    def stop_instance(self, instance_id: str) -> Instance: ...
    def terminate_instance(self, instance_id: str) -> bool: ...
    def list_gpus(self, gpu_count: int | None = None) -> list[GpuInfo]: ...
```

### 2. Register it

In `src/swm/providers/__init__.py`:

```python
from swm.providers.nebius import NebiusProvider
ALL_PROVIDERS: list[type[CloudProvider]] = [..., NebiusProvider]
```

### 3. Requirements for SSH compatibility

| Requirement | Why |
|-------------|-----|
| **Public IP + TCP port for SSH** | Direct SCP/rsync file transfers |
| **sshd inside the container** | The provider's Docker image must start sshd |
| **Environment variable injection** | `swm` passes `PUBLIC_KEY` at creation time |
| **Stop/resume** (optional) | Enables `swm pod stop` / `swm pod start` |

## Adding a New Storage Backend

Any S3-compatible storage provider can be added in minutes.

### 1. Create the storage module

Create `src/swm/storage/r2.py`:

```python
from swm import config as cfg
from swm.storage.base import BucketInfo, S3CompatProvider

class R2Provider(S3CompatProvider):
    _bucket_config_key = "r2.bucket"

    @property
    def name(self) -> str:
        return "Cloudflare R2"

    @property
    def slug(self) -> str:
        return "r2"

    def is_configured(self) -> bool:
        return bool(cfg.get("r2.access_key") and cfg.get("r2.secret_key"))

    def _s3_endpoint_url(self) -> str | None:
        account_id = cfg.get("r2.account_id")
        return f"https://{account_id}.r2.cloudflarestorage.com"

    def _s3_credentials(self) -> tuple[str | None, str | None]:
        return str(cfg.get("r2.access_key")), str(cfg.get("r2.secret_key"))

    def create_bucket(self, name, location="", storage_class="") -> BucketInfo:
        self.s3.create_bucket(Bucket=name)
        cfg.set_value("r2.bucket", name)
        return BucketInfo(provider=self.slug, name=name)
```

### 2. Register it

In `src/swm/storage/__init__.py`:

```python
from swm.storage.r2 import R2Provider
ALL_STORAGE: list[type[StorageProvider]] = [..., R2Provider]
```

### 3. Add s5cmd support

In `src/swm/bootstrap.py`, add the provider's slug to `_s3_env()` so it can build the correct env vars. Then wire it into `swm setup storage` and `swm pod create`'s bootstrap flow in `cli.py`.
