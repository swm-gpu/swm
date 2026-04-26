"""Live Docker image catalogs per provider, with on-disk caching.

The list of available images is fetched from each provider's actual source
of truth (e.g. Docker Hub for RunPod) so it can't drift. Results are cached
under ~/.config/swm/cache/ for 24h.

Currently only RunPod is implemented; other providers either don't have a
user-pickable image (Lambda, Vultr) or use a different concept (AMIs for
AWS, image families for GCP, templates for Vast.ai). Add more here as
needed.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

from swm.config import CONFIG_DIR

CACHE_DIR = CONFIG_DIR / "cache"
CACHE_TTL_SECONDS = 24 * 60 * 60

_RUNPOD_REPO = "runpod/pytorch"
_RUNPOD_HUB_URL = (
    f"https://hub.docker.com/v2/repositories/{_RUNPOD_REPO}/tags/"
    "?page_size=100&ordering=last_updated"
)

# Recognised tag styles:
#   legacy:  "2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
#   current: "1.0.3-cu1281-torch280-ubuntu2204"
_CUDA_RE = re.compile(r"cu(\d{3,4})|cuda(\d+\.\d+(?:\.\d+)?)")
_TORCH_RE = re.compile(r"torch(\d{3})|torch(\d+\.\d+\.\d+)")
_UBUNTU_RE = re.compile(r"ubuntu(\d{4}|\d{2}\.\d{2})")


@dataclass(frozen=True)
class ImageInfo:
    provider: str
    tag: str
    cuda: str  # "12.8" form
    torch: str | None  # "2.8.0" form, or None if unknown
    ubuntu: str  # "22.04" form
    last_updated: str  # ISO-8601 from Docker Hub, or "" if unknown


def _format_cu_short(s: str) -> str:
    """'1281' -> '12.8.1', '128' -> '12.8'."""
    if len(s) == 4:
        return f"{s[:2]}.{s[2]}.{s[3]}"
    if len(s) == 3:
        return f"{s[:2]}.{s[2]}"
    return s


def _cuda_major_minor(cuda: str) -> str:
    """'12.8.1' -> '12.8'."""
    parts = cuda.split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else cuda


def _format_torch_short(s: str) -> str:
    """'280' -> '2.8.0'."""
    if len(s) == 3:
        return f"{s[0]}.{s[1]}.{s[2]}"
    return s


def _format_ubuntu(s: str) -> str:
    """'2204' -> '22.04'; '22.04' stays as-is."""
    if "." in s:
        return s
    if len(s) == 4:
        return f"{s[:2]}.{s[2:]}"
    return s


def _parse_runpod_tag(tag: str) -> tuple[str, str | None, str] | None:
    """Return (cuda, torch, ubuntu) or None if the tag doesn't match."""
    m_cu = _CUDA_RE.search(tag)
    if not m_cu:
        return None
    cu_short, cu_long = m_cu.group(1), m_cu.group(2)
    cuda = _format_cu_short(cu_short) if cu_short else cu_long

    m_t = _TORCH_RE.search(tag)
    if m_t:
        t_short, t_long = m_t.group(1), m_t.group(2)
        torch = _format_torch_short(t_short) if t_short else t_long
    else:
        torch = None

    m_u = _UBUNTU_RE.search(tag)
    if not m_u:
        return None
    ubuntu = _format_ubuntu(m_u.group(1))
    return cuda, torch, ubuntu


def _cache_path(provider: str) -> Path:
    return CACHE_DIR / f"images-{provider}.json"


def _load_cache(provider: str) -> list[ImageInfo] | None:
    p = _cache_path(provider)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if (time.time() - data.get("ts", 0)) > CACHE_TTL_SECONDS:
        return None
    return [ImageInfo(**rec) for rec in data.get("images", [])]


def _save_cache(provider: str, images: list[ImageInfo]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": time.time(),
        "images": [asdict(i) for i in images],
    }
    _cache_path(provider).write_text(json.dumps(payload, indent=2))


def _fetch_runpod_images() -> list[ImageInfo]:
    out: list[ImageInfo] = []
    url: str | None = _RUNPOD_HUB_URL
    pages = 0
    with httpx.Client(timeout=15.0) as client:
        while url and pages < 5:
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.json()
            for item in data.get("results", []):
                tag = item.get("name", "")
                # Skip dev/preview tags for the default catalog
                if not tag or "-dev-" in tag:
                    continue
                parsed = _parse_runpod_tag(tag)
                if not parsed:
                    continue
                cuda, torch, ubuntu = parsed
                out.append(
                    ImageInfo(
                        provider="runpod",
                        tag=f"{_RUNPOD_REPO}:{tag}",
                        cuda=cuda,
                        torch=torch,
                        ubuntu=ubuntu,
                        last_updated=item.get("last_updated", "") or "",
                    )
                )
            url = data.get("next")
            pages += 1
    out.sort(key=lambda i: i.last_updated, reverse=True)
    return out


def list_images(provider: str, *, refresh: bool = False) -> list[ImageInfo]:
    """Return available images for `provider`, hitting cache when fresh."""
    if not refresh:
        cached = _load_cache(provider)
        if cached is not None:
            return cached

    if provider == "runpod":
        images = _fetch_runpod_images()
    else:
        images = []

    if images:
        _save_cache(provider, images)
    return images


def parse_image_cuda(image: str) -> str | None:
    """Best-effort extraction of a CUDA major.minor[.patch] from an image tag.

    Returns None if no CUDA marker is present in the tag.
    """
    m = _CUDA_RE.search(image)
    if not m:
        return None
    cu_short, cu_long = m.group(1), m.group(2)
    return _format_cu_short(cu_short) if cu_short else cu_long


def resolve_image(provider: str, cuda: str, *, refresh: bool = False) -> str | None:
    """Resolve `--cuda X.Y` to the most recently-updated image tag.

    Returns None if no matching image exists for the provider.
    """
    target = _cuda_major_minor(cuda)
    for img in list_images(provider, refresh=refresh):
        if _cuda_major_minor(img.cuda) == target:
            return img.tag
    return None
