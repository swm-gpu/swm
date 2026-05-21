"""HuggingFace Hub API client for model search and metadata."""
from __future__ import annotations

import os
import re
import warnings
from dataclasses import dataclass, field

import httpx

from swm import config as cfg

_API = "https://huggingface.co/api"
_TIMEOUT = 20
_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[Bb]")


def get_token(override: str | None = None) -> str | None:
    """Resolve the HuggingFace token from CLI > config > legacy > env.

    Lookup order:
      1. *override* (passed via ``--token`` flag)
      2. ``hf.api_key`` config key  (canonical, matches GPU providers)
      3. ``hf_token`` config key    (legacy, emits a deprecation warning)
      4. ``HF_TOKEN`` environment variable
    """
    if override:
        return override
    val = cfg.get("hf.api_key")
    if val:
        return str(val)
    legacy = cfg.get("hf_token")
    if legacy:
        warnings.warn(
            "config key `hf_token` is deprecated; "
            "migrate with `swm config set hf.api_key <token>`",
            DeprecationWarning,
            stacklevel=2,
        )
        return str(legacy)
    env = os.environ.get("HF_TOKEN")
    return env if env else None


@dataclass
class ModelResult:
    model_id: str
    author: str
    downloads: int
    likes: int
    gated: bool | str
    pipeline: str
    library: str
    tags: list[str] = field(default_factory=list)
    last_modified: str = ""

    @property
    def is_gguf(self) -> bool:
        return "gguf" in (t.lower() for t in self.tags)

    @property
    def size_label(self) -> str:
        """Extract a size hint like '8B' or '72B' from the model ID."""
        m = _SIZE_RE.search(self.model_id)
        return f"{m.group(1)}B" if m else ""


def search(
    query: str,
    *,
    sort: str = "downloads",
    limit: int = 15,
    pipeline: str | None = "text-generation",
) -> list[ModelResult]:
    """Search HuggingFace Hub for models matching *query*."""
    params: dict[str, str | int] = {
        "search": query,
        "sort": sort,
        "direction": "-1",
        "limit": limit,
    }
    if pipeline:
        params["filter"] = pipeline

    resp = httpx.get(f"{_API}/models", params=params, timeout=_TIMEOUT)
    resp.raise_for_status()

    return [
        ModelResult(
            model_id=m.get("id", ""),
            author=m.get("author", ""),
            downloads=m.get("downloads", 0),
            likes=m.get("likes", 0),
            gated=m.get("gated", False),
            pipeline=m.get("pipeline_tag", ""),
            library=m.get("library_name", ""),
            tags=m.get("tags", []),
            last_modified=m.get("lastModified", ""),
        )
        for m in resp.json()
    ]


def model_info(model_id: str, *, token: str | None = None) -> dict:
    """Fetch full metadata for a single model."""
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = httpx.get(
        f"{_API}/models/{model_id}",
        headers=headers,
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def model_exists(model_id: str, *, token: str | None = None) -> bool:
    """Return True if *model_id* resolves to a public/accessible HF model."""
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = httpx.head(
            f"{_API}/models/{model_id}",
            headers=headers,
            timeout=_TIMEOUT,
            follow_redirects=True,
        )
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


def find_gguf_mirror(ollama_ref: str, *, token: str | None = None) -> str | None:
    """Locate a HuggingFace GGUF mirror for an Ollama-style reference.

    Many Ollama models are hosted as GGUFs by ``bartowski`` on HuggingFace.
    This helper takes ``deepseek-r1:14b`` and returns
    ``bartowski/DeepSeek-R1-Distill-Qwen-14B-GGUF`` when it exists, so the
    pod can fall back to a direct HF download when the Ollama binary is
    unavailable.

    Heuristic only -- returns ``None`` when no obvious mirror is found.
    """
    base, _, _tag = ollama_ref.partition(":")
    if not base:
        return None
    needle = base.replace("-", " ").replace("_", " ")
    try:
        results = search(needle, sort="downloads", limit=10, pipeline=None)
    except httpx.HTTPError:
        return None
    for r in results:
        if r.is_gguf and base.lower().replace("-", "") in r.model_id.lower().replace("-", ""):
            return r.model_id
    return None
