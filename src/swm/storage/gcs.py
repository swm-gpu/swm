from __future__ import annotations

import shutil
import subprocess

from swm import config as cfg
from swm.storage.base import BucketInfo, S3CompatProvider


class GCSProvider(S3CompatProvider):
    _bucket_config_key = "gcp.bucket"

    @property
    def name(self) -> str:
        return "Google Cloud Storage"

    @property
    def slug(self) -> str:
        return "gcs"

    def _project(self) -> str:
        project = cfg.get("gcp.project")
        if not project:
            raise RuntimeError("GCP project not set. Run: swm config set gcp.project <id>")
        return str(project)

    def is_configured(self) -> bool:
        return bool(cfg.get("gcs.hmac_access") and cfg.get("gcs.hmac_secret"))

    def _s3_endpoint_url(self) -> str | None:
        return "https://storage.googleapis.com"

    def _s3_credentials(self) -> tuple[str | None, str | None]:
        hmac_access = cfg.get("gcs.hmac_access")
        hmac_secret = cfg.get("gcs.hmac_secret")
        if not hmac_access or not hmac_secret:
            raise RuntimeError(
                "GCS HMAC keys not set. Create them with:\n"
                "  gcloud storage hmac create <service-account-email> --project=<project>\n"
                "Then:\n"
                "  swm config set gcs.hmac_access <access-id>\n"
                "  swm config set gcs.hmac_secret <secret>"
            )
        return str(hmac_access), str(hmac_secret)

    def create_bucket(
        self,
        name: str,
        location: str = "",
        storage_class: str = "",
    ) -> BucketInfo:
        location = location or "us-central1"
        storage_class = storage_class or "STANDARD"

        gcloud = shutil.which("gcloud")
        if not gcloud:
            raise RuntimeError("gcloud CLI not found on PATH.")
        subprocess.run(
            [
                gcloud, "storage", "buckets", "create", f"gs://{name}",
                f"--project={self._project()}",
                f"--location={location}",
                f"--default-storage-class={storage_class}",
                "--uniform-bucket-level-access",
            ],
            check=True, capture_output=True, text=True, timeout=120,
        )
        cfg.set_value("gcp.bucket", name)
        return BucketInfo(
            provider=self.slug,
            name=name,
            location=location,
            storage_class=storage_class,
        )
