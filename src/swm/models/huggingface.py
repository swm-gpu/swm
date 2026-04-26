"""HuggingFace Hub API client for model search and metadata."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import httpx

_API = "https://huggingface.co/api"
_TIMEOUT = 20
_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[Bb]")


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
