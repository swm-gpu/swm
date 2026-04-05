from __future__ import annotations

import json
import subprocess

from swm import config as cfg
from swm.storage.base import BucketInfo, S3CompatProvider


def _detect_s3_endpoint() -> str | None:
    """Auto-detect B2 S3 endpoint from b2 CLI account info, then cache it."""
    import shutil

    b2 = shutil.which("b2")
    if not b2:
        return None
    try:
        result = subprocess.run(
            [b2, "account", "get"],
            capture_output=True, text=True, timeout=10,
        )
        info = json.loads(result.stdout.strip() or "{}")
        endpoint = info.get("s3endpoint")
        if endpoint:
            cfg.set_value("b2.s3_endpoint", endpoint)
        return endpoint
    except Exception:
        return None


class B2Provider(S3CompatProvider):
    _bucket_config_key = "b2.bucket"

    @property
    def name(self) -> str:
        return "Backblaze B2"

    @property
    def slug(self) -> str:
        return "b2"

    def is_configured(self) -> bool:
        return bool(cfg.get("b2.key_id") and cfg.get("b2.app_key"))

    def _s3_endpoint_url(self) -> str | None:
        endpoint = cfg.get("b2.s3_endpoint")
        if endpoint:
            return str(endpoint)
        detected = _detect_s3_endpoint()
        if detected:
            return detected
        raise RuntimeError(
            "B2 S3 endpoint unknown. Set it with:\n"
            "  swm config set b2.s3_endpoint https://s3.<region>.backblazeb2.com"
        )

    def _s3_credentials(self) -> tuple[str | None, str | None]:
        return (
            str(cfg.get("b2.key_id") or ""),
            str(cfg.get("b2.app_key") or ""),
        )

    def list_buckets(self) -> list[BucketInfo]:
        try:
            return super().list_buckets()
        except Exception:
            default = self.default_bucket()
            if default:
                return [BucketInfo(provider=self.slug, name=default)]
            raise

    def create_bucket(
        self,
        name: str,
        location: str = "",
        storage_class: str = "",
    ) -> BucketInfo:
        bucket_type = storage_class or "allPrivate"
        self.s3.create_bucket(
            Bucket=name,
            CreateBucketConfiguration={"LocationConstraint": location or ""},
        )
        cfg.set_value("b2.bucket", name)
        return BucketInfo(
            provider=self.slug,
            name=name,
            storage_class=bucket_type,
        )
