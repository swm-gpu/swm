"""Resolve a user-supplied model reference to a source, canonical id, and asset type.

The resolver is deliberately pure-Python and lookup-free for the common cases
(``org/name`` -> HuggingFace, ``name:tag`` -> Ollama, Civitai refs, URLs).  Only
ambiguous references (single-segment names like ``qwen3-8b``) trigger a
network registry lookup.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from swm.models import civitai, huggingface

# Asset-type vocabulary -> on-pod directory under /workspace/models/.
ASSET_DIRS: dict[str, str] = {
    "llm": "hf",
    "llm-gguf": "files",
    "ollama": "ollama",
    "checkpoint": "checkpoints",
    "lora": "loras",
    "vae": "vae",
    "controlnet": "controlnet",
    "embedding": "embeddings",
    "clip": "clip",
    "clip-vision": "clip_vision",
    "upscaler": "upscale_models",
    "unet": "unet",
    "diffusion": "diffusion_models",
    "text-encoder": "text_encoders",
    "file": "files",
}

# Asset types that ComfyUI / SwarmUI expose through a bucket-style directory.
COMFYUI_TYPES = {
    "checkpoint", "lora", "vae", "controlnet", "embedding",
    "clip", "clip-vision", "upscaler", "unet", "diffusion", "text-encoder",
}

VALID_SOURCES = {"hf", "ollama", "civitai", "url"}


@dataclass
class Resolved:
    """Result of resolving a user-supplied ref."""

    source: str  # "hf" | "ollama" | "civitai" | "url"
    ref: str  # canonical reference (e.g. "Qwen/Qwen3-8B")
    asset_type: str  # swm asset-type vocab
    display_name: str  # human-friendly name (often == ref)
    needs_engine: str | None = None  # framework name required to consume this
    extra: dict | None = None  # source-specific payload (civitai version etc.)


_HF_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_OLLAMA_RE = re.compile(r"^[A-Za-z0-9._-]+(?::[A-Za-z0-9._-]+)?$")
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def _default_type_for_hf(info: dict) -> str:
    """Pick a default asset type from HF model metadata.

    Heuristic priority:
      1. GGUF tag -> ``llm-gguf``
      2. pipeline_tag ``text-generation`` -> ``llm``
      3. library_name ``diffusers`` -> ``checkpoint``
      4. anything else -> ``llm`` (HF cache fallback)
    """
    tags = {t.lower() for t in info.get("tags", [])}
    if "gguf" in tags:
        return "llm-gguf"
    pipeline = info.get("pipeline_tag") or ""
    if pipeline == "text-generation":
        return "llm"
    library = (info.get("library_name") or "").lower()
    if library == "diffusers" or "diffusion" in pipeline:
        return "checkpoint"
    return "llm"


def resolve(
    ref: str,
    *,
    source: str | None = None,
    asset_type: str | None = None,
    hf_token: str | None = None,
    civitai_token: str | None = None,
) -> Resolved:
    """Resolve a user ref to a :class:`Resolved` descriptor.

    *source* (``hf|ollama|civitai|url``) and *asset_type* override the
    auto-detected values.  ``ValueError`` is raised when the ref can't be
    resolved.
    """
    ref = ref.strip()
    if not ref:
        raise ValueError("empty model reference")

    if source is not None and source not in VALID_SOURCES:
        raise ValueError(
            f"unknown --source {source!r}; expected one of {sorted(VALID_SOURCES)}"
        )

    if asset_type is not None and asset_type not in ASSET_DIRS:
        raise ValueError(
            f"unknown --as {asset_type!r}; expected one of {sorted(ASSET_DIRS)}"
        )

    explicit_source = source

    if explicit_source == "civitai" or (
        explicit_source is None and civitai.is_civitai_ref(ref)
    ):
        return _resolve_civitai(ref, asset_type=asset_type, token=civitai_token)

    if explicit_source == "url" or (
        explicit_source is None and _URL_RE.match(ref)
    ):
        return _resolve_url(ref, asset_type=asset_type)

    if explicit_source == "hf":
        return _resolve_hf(ref, asset_type=asset_type, token=hf_token)

    if explicit_source == "ollama":
        return _resolve_ollama(ref, asset_type=asset_type)

    # Auto-detect by ref shape.
    if _HF_RE.match(ref):
        return _resolve_hf(ref, asset_type=asset_type, token=hf_token)

    if ":" in ref and "/" not in ref:
        return _resolve_ollama(ref, asset_type=asset_type)

    # Ambiguous single segment -> parallel registry lookup.
    return _resolve_ambiguous(ref, asset_type=asset_type, hf_token=hf_token)


def _resolve_hf(ref: str, *, asset_type: str | None, token: str | None) -> Resolved:
    import httpx

    try:
        info = huggingface.model_info(ref, token=token)
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status == 404:
            raise ValueError(
                f"HuggingFace repo {ref!r} not found. "
                "Check the org/name and try `swm models search`."
            ) from exc
        if status in (401, 403):
            raise ValueError(
                f"HuggingFace repo {ref!r} is gated or requires auth. "
                "Run: swm config set hf.api_key <token>"
            ) from exc
        raise ValueError(
            f"HuggingFace returned HTTP {status} for {ref!r}: {exc.response.text[:200]}"
        ) from exc
    except httpx.HTTPError as exc:
        raise ValueError(
            f"Network error while resolving {ref!r}: {exc}"
        ) from exc
    detected = asset_type or _default_type_for_hf(info)
    needs = {
        "llm": "vllm",
        "llm-gguf": "ollama",
        "checkpoint": "comfyui",
        "lora": "comfyui",
        "vae": "comfyui",
        "controlnet": "comfyui",
        "embedding": "comfyui",
    }.get(detected)
    return Resolved(
        source="hf",
        ref=info.get("id", ref),
        asset_type=detected,
        display_name=info.get("id", ref),
        needs_engine=needs,
        extra={"hf_info": info},
    )


def _resolve_ollama(ref: str, *, asset_type: str | None) -> Resolved:
    return Resolved(
        source="ollama",
        ref=ref if ":" in ref else f"{ref}:latest",
        asset_type=asset_type or "ollama",
        display_name=ref,
        needs_engine="ollama",
    )


def _resolve_civitai(
    ref: str, *, asset_type: str | None, token: str | None
) -> Resolved:
    import httpx

    try:
        info = civitai.model_info(ref, token=token)
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status == 404:
            raise ValueError(f"Civitai model not found: {ref!r}") from exc
        if status in (401, 403):
            raise ValueError(
                f"Civitai model {ref!r} requires auth. "
                "Run: swm config set civitai.api_key <token>"
            ) from exc
        raise ValueError(
            f"Civitai returned HTTP {status} for {ref!r}"
        ) from exc
    except httpx.HTTPError as exc:
        raise ValueError(f"Network error while resolving {ref!r}: {exc}") from exc
    _model_id, version_id = civitai.parse_ref(ref) if ref.startswith("civitai:") else (
        info.model_id, civitai.parse_ref(ref)[1] if "modelVersionId=" in ref else None,
    )
    version = info.primary_version(version_id)
    canonical = f"civitai:{info.model_id}:{version.version_id}"
    detected = asset_type or info.asset_type
    return Resolved(
        source="civitai",
        ref=canonical,
        asset_type=detected,
        display_name=f"{info.creator}/{info.name}" if info.creator else info.name,
        needs_engine="comfyui",
        extra={"civitai_model": info, "version_id": version.version_id},
    )


def _resolve_url(ref: str, *, asset_type: str | None) -> Resolved:
    # The user is telling us "fetch this single file"; we can't introspect.
    return Resolved(
        source="url",
        ref=ref,
        asset_type=asset_type or "file",
        display_name=ref.rsplit("/", 1)[-1] or ref,
        needs_engine=None,
    )


def _resolve_ambiguous(
    ref: str, *, asset_type: str | None, hf_token: str | None
) -> Resolved:
    """Look up the ref against HF + Ollama registries to disambiguate."""
    import concurrent.futures

    def _hf() -> str | None:
        try:
            results = huggingface.search(ref, limit=5, pipeline=None)
        except Exception:
            return None
        # Exact (case-insensitive) match on basename wins.
        for r in results:
            base = r.model_id.split("/", 1)[-1]
            if base.lower() == ref.lower():
                return r.model_id
        return None

    def _ollama() -> str | None:
        # Ollama's library doesn't have a public search API; we check the
        # GitHub mirror that the Ollama UI uses.
        import httpx

        try:
            resp = httpx.get(
                "https://ollama-models.zwz.workers.dev/",
                params={"search": ref},
                timeout=10,
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
        except Exception:
            return None
        if not isinstance(data, list):
            return None
        for item in data:
            name = (item or {}).get("name", "")
            if name.lower() == ref.lower():
                return name
        return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        hf_future = pool.submit(_hf)
        ollama_future = pool.submit(_ollama)
        hf_hit = hf_future.result()
        ollama_hit = ollama_future.result()

    if hf_hit and ollama_hit:
        raise ValueError(
            f"{ref!r} matches both HuggingFace ({hf_hit}) and Ollama "
            f"({ollama_hit}); pass --source hf|ollama to disambiguate"
        )
    if hf_hit:
        return _resolve_hf(hf_hit, asset_type=asset_type, token=hf_token)
    if ollama_hit:
        return _resolve_ollama(ollama_hit, asset_type=asset_type)
    raise ValueError(
        f"could not resolve {ref!r} on HuggingFace or Ollama; "
        "pass org/name (HF) or name:tag (Ollama) or use --source"
    )


def target_dir_for(asset_type: str) -> str:
    """Return the on-pod subdirectory under ``/workspace/models/`` for *asset_type*."""
    if asset_type not in ASSET_DIRS:
        raise ValueError(f"unknown asset type {asset_type!r}")
    return ASSET_DIRS[asset_type]
