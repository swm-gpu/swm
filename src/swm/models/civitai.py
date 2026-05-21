"""Civitai API client for SD/SDXL checkpoints, LoRAs, embeddings, VAEs.

Civitai's REST API is documented at https://github.com/civitai/civitai/wiki/REST-API-Reference.

Reference formats accepted by swm:
  * Integer model id        -> ``civitai:101055``                   (resolves latest version)
  * Model + version id      -> ``civitai:101055:128713``            (pinned version)
  * Web URL                 -> ``https://civitai.com/models/101055``
  * Web URL with version    -> ``https://civitai.com/models/101055?modelVersionId=128713``
"""
from __future__ import annotations

import os
import re
import warnings
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import httpx

from swm import config as cfg

_API = "https://civitai.com/api/v1"
_TIMEOUT = 20

# Civitai's model "type" field -> swm asset-type vocabulary.
TYPE_MAP = {
    "Checkpoint": "checkpoint",
    "LORA": "lora",
    "LoCon": "lora",
    "DoRA": "lora",
    "TextualInversion": "embedding",
    "Hypernetwork": "embedding",
    "AestheticGradient": "embedding",
    "VAE": "vae",
    "Controlnet": "controlnet",
    "Upscaler": "upscaler",
    "MotionModule": "diffusion",
    "Other": "checkpoint",
}


def get_token(override: str | None = None) -> str | None:
    """Resolve a Civitai API key from CLI > config > env.

    Lookup order mirrors the GPU-provider pattern:
      1. *override* (``--token``)
      2. ``civitai.api_key`` config key
      3. ``CIVITAI_API_KEY`` environment variable
    """
    if override:
        return override
    val = cfg.get("civitai.api_key")
    if val:
        return str(val)
    env = os.environ.get("CIVITAI_API_KEY")
    return env if env else None


@dataclass
class CivitaiFile:
    name: str
    download_url: str
    size_kb: int
    file_type: str  # e.g. "Model", "VAE", "Config"


@dataclass
class CivitaiVersion:
    version_id: int
    name: str
    base_model: str
    files: list[CivitaiFile]


@dataclass
class CivitaiModel:
    model_id: int
    name: str
    creator: str
    asset_type: str  # swm vocab (e.g. "checkpoint")
    nsfw: bool
    versions: list[CivitaiVersion]

    def primary_version(self, version_id: int | None = None) -> CivitaiVersion:
        if version_id is not None:
            for v in self.versions:
                if v.version_id == version_id:
                    return v
            raise ValueError(
                f"version {version_id} not found on civitai model {self.model_id}"
            )
        if not self.versions:
            raise ValueError(f"civitai model {self.model_id} has no versions")
        return self.versions[0]


_URL_RE = re.compile(r"https?://civitai\.com/models/(\d+)", re.IGNORECASE)
_REF_RE = re.compile(r"^civitai:(\d+)(?::(\d+))?$", re.IGNORECASE)


def parse_ref(ref: str) -> tuple[int, int | None]:
    """Parse a Civitai ref into ``(model_id, version_id_or_None)``.

    Raises ``ValueError`` for unrecognised input.
    """
    m = _REF_RE.match(ref.strip())
    if m:
        return int(m.group(1)), int(m.group(2)) if m.group(2) else None
    if ref.startswith(("http://", "https://")):
        u = urlparse(ref)
        path_match = re.match(r"/models/(\d+)", u.path)
        if path_match:
            model_id = int(path_match.group(1))
            qs = parse_qs(u.query)
            v = qs.get("modelVersionId", [None])[0]
            return model_id, int(v) if v else None
    raise ValueError(f"unrecognised Civitai reference: {ref!r}")


def is_civitai_ref(ref: str) -> bool:
    """Return True if *ref* looks like a Civitai reference."""
    if _REF_RE.match(ref.strip()):
        return True
    if ref.startswith(("http://", "https://")) and "civitai.com/models/" in ref.lower():
        return True
    return False


def model_info(ref: str, *, token: str | None = None) -> CivitaiModel:
    """Fetch metadata for a Civitai model by reference."""
    model_id, _vid = parse_ref(ref)
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = httpx.get(
        f"{_API}/models/{model_id}",
        headers=headers,
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()

    raw_type = data.get("type", "Other")
    if raw_type not in TYPE_MAP:
        warnings.warn(
            f"unknown civitai type {raw_type!r}; defaulting to 'checkpoint'"
        )
    asset_type = TYPE_MAP.get(raw_type, "checkpoint")

    versions: list[CivitaiVersion] = []
    for v in data.get("modelVersions", []):
        files: list[CivitaiFile] = []
        for f in v.get("files", []):
            files.append(
                CivitaiFile(
                    name=f.get("name", ""),
                    download_url=f.get("downloadUrl", ""),
                    size_kb=int(f.get("sizeKB", 0)),
                    file_type=f.get("type", ""),
                )
            )
        versions.append(
            CivitaiVersion(
                version_id=int(v.get("id", 0)),
                name=v.get("name", ""),
                base_model=v.get("baseModel", ""),
                files=files,
            )
        )

    return CivitaiModel(
        model_id=int(data.get("id", model_id)),
        name=data.get("name", ""),
        creator=(data.get("creator") or {}).get("username", ""),
        asset_type=asset_type,
        nsfw=bool(data.get("nsfw", False)),
        versions=versions,
    )


def primary_download(
    model: CivitaiModel,
    version_id: int | None = None,
    token: str | None = None,
) -> tuple[CivitaiFile, str]:
    """Pick the primary (Model-type) file and return ``(file, authed_url)``.

    The returned URL embeds a ``token`` query param if one is configured, so
    the remote pod can ``curl`` it without further auth setup.
    """
    version = model.primary_version(version_id)
    primary = next(
        (f for f in version.files if f.file_type == "Model"),
        version.files[0] if version.files else None,
    )
    if primary is None:
        raise ValueError(
            f"civitai model {model.model_id} version {version.version_id} has no files"
        )
    url = primary.download_url
    tok = token or get_token()
    if tok:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}token={tok}"
    return primary, url
