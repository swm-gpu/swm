from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from functools import cached_property


@dataclass
class BucketInfo:
    provider: str
    name: str
    location: str = ""
    storage_class: str = ""
    created: str = ""


@dataclass
class ObjectInfo:
    key: str
    size: int = 0
    modified: str = ""
    is_directory: bool = False

    @property
    def size_display(self) -> str:
        if self.is_directory:
            return "DIR"
        s = float(self.size)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if s < 1024:
                return f"{s:.1f} {unit}" if unit != "B" else f"{int(s)} B"
            s /= 1024
        return f"{s:.1f} PB"


class StorageProvider(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def slug(self) -> str: ...

    @abstractmethod
    def is_configured(self) -> bool: ...

    @abstractmethod
    def default_bucket(self) -> str | None: ...

    @abstractmethod
    def list_buckets(self) -> list[BucketInfo]: ...

    @abstractmethod
    def create_bucket(
        self,
        name: str,
        location: str = "",
        storage_class: str = "",
    ) -> BucketInfo: ...

    @abstractmethod
    def ls(self, bucket: str, prefix: str = "") -> list[ObjectInfo]: ...

    @abstractmethod
    def upload(self, local_path: str, bucket: str, remote_path: str) -> None: ...

    @abstractmethod
    def download(self, bucket: str, remote_path: str, local_path: str) -> None: ...


class S3CompatProvider(StorageProvider):
    """Base for storage providers accessible via the S3-compatible API (boto3).

    Subclasses only need to provide:
      - name, slug              (identity)
      - _s3_endpoint_url()      (None for native AWS S3)
      - _s3_credentials()       (access_key, secret_key)
      - _bucket_config_key      (config key for default bucket)
      - is_configured()         (credential check)
      - create_bucket()         (provider-specific options)
    """

    _bucket_config_key: str = ""

    @abstractmethod
    def _s3_endpoint_url(self) -> str | None:
        """Return the S3-compatible endpoint URL, or None for native AWS."""
        ...

    @abstractmethod
    def _s3_credentials(self) -> tuple[str | None, str | None]:
        """Return (access_key_id, secret_access_key), or (None, None) for env/profile."""
        ...

    @cached_property
    def s3(self):
        """Cached boto3 S3 client."""
        import boto3

        kw: dict = {}
        endpoint = self._s3_endpoint_url()
        if endpoint:
            kw["endpoint_url"] = endpoint
        ak, sk = self._s3_credentials()
        if ak and sk:
            kw["aws_access_key_id"] = ak
            kw["aws_secret_access_key"] = sk
        return boto3.client("s3", **kw)

    def default_bucket(self) -> str | None:
        from swm import config as cfg
        val = cfg.get(self._bucket_config_key)
        return str(val) if val else None

    def list_buckets(self) -> list[BucketInfo]:
        resp = self.s3.list_buckets()
        return [
            BucketInfo(
                provider=self.slug,
                name=b["Name"],
                created=str(b.get("CreationDate", ""))[:10],
            )
            for b in resp.get("Buckets", [])
        ]

    def ls(self, bucket: str, prefix: str = "") -> list[ObjectInfo]:
        paginator = self.s3.get_paginator("list_objects_v2")
        results: list[ObjectInfo] = []
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
            for cp in page.get("CommonPrefixes", []):
                key = cp["Prefix"]
                if prefix:
                    key = key.removeprefix(prefix).lstrip("/")
                if key:
                    results.append(ObjectInfo(key=key, is_directory=True))
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if prefix:
                    key = key.removeprefix(prefix).lstrip("/")
                if not key:
                    continue
                modified = str(obj.get("LastModified", ""))[:19]
                results.append(ObjectInfo(
                    key=key,
                    size=obj.get("Size", 0),
                    modified=modified,
                ))
        return results

    def upload(self, local_path: str, bucket: str, remote_path: str) -> None:
        self.s3.upload_file(local_path, bucket, remote_path)

    def download(self, bucket: str, remote_path: str, local_path: str) -> None:
        self.s3.download_file(bucket, remote_path, local_path)
