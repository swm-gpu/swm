from __future__ import annotations

from swm import config as cfg
from swm.storage.base import BucketInfo, S3CompatProvider


class S3Provider(S3CompatProvider):
    _bucket_config_key = "s3.bucket"

    @property
    def name(self) -> str:
        return "Amazon S3"

    @property
    def slug(self) -> str:
        return "s3"

    def _region(self) -> str:
        return str(cfg.get("aws.region", "us-east-1"))

    def is_configured(self) -> bool:
        ak = cfg.get("s3.access_key")
        sk = cfg.get("s3.secret_key")
        if ak and sk:
            return True
        try:
            self.s3.list_buckets()
            return True
        except Exception:
            return False

    def _s3_endpoint_url(self) -> str | None:
        return None

    def _s3_credentials(self) -> tuple[str | None, str | None]:
        ak = cfg.get("s3.access_key")
        sk = cfg.get("s3.secret_key")
        if ak and sk:
            return str(ak), str(sk)
        return None, None

    def create_bucket(
        self,
        name: str,
        location: str = "",
        storage_class: str = "",
    ) -> BucketInfo:
        location = location or self._region()
        kw: dict = {"Bucket": name}
        if location != "us-east-1":
            kw["CreateBucketConfiguration"] = {"LocationConstraint": location}
        self.s3.create_bucket(**kw)
        cfg.set_value("s3.bucket", name)
        return BucketInfo(
            provider=self.slug,
            name=name,
            location=location,
            storage_class=storage_class or "STANDARD",
        )
