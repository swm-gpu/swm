from __future__ import annotations

from swm.storage.base import BucketInfo, ObjectInfo, S3CompatProvider, StorageProvider
from swm.storage.gcs import GCSProvider
from swm.storage.b2 import B2Provider
from swm.storage.s3 import S3Provider

ALL_STORAGE: list[type[StorageProvider]] = [
    GCSProvider,
    B2Provider,
    S3Provider,
]

STORAGE_SLUGS = {cls().slug: cls for cls in ALL_STORAGE}


def get_storage(slug: str) -> StorageProvider:
    cls = STORAGE_SLUGS.get(slug)
    if cls is None:
        raise ValueError(
            f"Unknown storage provider '{slug}'. "
            f"Available: {', '.join(STORAGE_SLUGS)}"
        )
    return cls()


def get_configured_storage() -> list[StorageProvider]:
    result: list[StorageProvider] = []
    for cls in ALL_STORAGE:
        p = cls()
        try:
            if p.is_configured():
                result.append(p)
        except Exception:
            pass
    return result


def resolve_bucket(spec: str | None = None) -> tuple[StorageProvider, str]:
    """Resolve 'provider:bucket' or bare 'bucket' to (provider, bucket_name).

    Resolution order:
      1. Explicit 'provider:bucket' spec
      2. Global default from ``storage.default`` config (e.g. "b2:my-bucket")
      3. First configured provider with a default bucket
    """
    if spec and ":" in spec:
        slug, bucket = spec.split(":", 1)
        return get_storage(slug), bucket

    from swm import config as cfg

    global_default = cfg.get("storage.default")
    if global_default and ":" in str(global_default):
        slug, bucket = str(global_default).split(":", 1)
        try:
            return get_storage(slug), bucket
        except ValueError:
            pass

    for p in get_configured_storage():
        if spec:
            buckets = p.list_buckets()
            if any(b.name == spec for b in buckets):
                return p, spec

    for p in get_configured_storage():
        default = p.default_bucket()
        if default:
            return p, default

    raise ValueError(
        "No bucket specified and no default configured. "
        "Use 'provider:bucket' format or set a default with "
        "'swm config set storage.default b2:<bucket>'."
    )


__all__ = [
    "BucketInfo",
    "ObjectInfo",
    "StorageProvider",
    "S3CompatProvider",
    "GCSProvider",
    "B2Provider",
    "S3Provider",
    "ALL_STORAGE",
    "STORAGE_SLUGS",
    "get_storage",
    "get_configured_storage",
    "resolve_bucket",
]
