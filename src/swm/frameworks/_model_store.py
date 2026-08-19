"""Shared shell for wiring a framework's model directory to the unified store.

Three copies of this script — comfyui.py, swarmui.py, bootstrap_frameworks.py —
had already drifted into triplication, and each carried its own copy of the
diffusion bucket list. The set of buckets a framework links is exactly the set
of asset types it declares in ``Framework.consumes``, so the script is now
generated from that declaration and the list exists in one place.

The emitted bash is byte-identical to what the three copies produced (verified
by string comparison in tests): it migrates anything already sitting in the
framework's own model dirs into the store before symlinking, and is idempotent.
"""

from __future__ import annotations

# Asset types (resolver vocabulary) that ComfyUI-style diffusion stacks load
# from the unified store, and the store bucket each maps to. This is the list
# that used to exist in three drifting copies.
DIFFUSION_CONSUMES: frozenset[str] = frozenset({
    "checkpoint", "lora", "vae", "controlnet", "embedding",
    "clip", "clip-vision", "upscaler", "unet", "diffusion", "text-encoder",
})

# Bucket order matters: the emitted shell must stay byte-identical to what the
# previous per-file copies produced, so this keeps their exact ordering rather
# than deriving (unordered) from the set above.
DIFFUSION_BUCKETS: list[str] = [
    "checkpoints", "loras", "vae", "controlnet", "embeddings",
    "clip", "clip_vision", "upscale_models", "unet",
    "diffusion_models", "text_encoders",
]


def link_store_script(model_root: str, buckets: list[str]) -> str:
    """Bash that points ``<model_root>/<bucket>`` at ``/workspace/models/<bucket>``.

    Preserves anything already sitting under the framework's own dirs by moving
    it into the store before replacing with a symlink. Idempotent: re-running
    is a no-op once the symlinks exist.
    """
    parts = [
        "mkdir -p /workspace/models/{" + ",".join(buckets) + "}",
        f"mkdir -p {model_root}",
    ]
    for d in buckets:
        target = f"{model_root}/{d}"
        store = f"/workspace/models/{d}"
        parts.append(
            f"if [ -L {target} ]; then :; "
            f"elif [ -d {target} ]; then "
            f"  ( shopt -s dotglob nullglob; mv {target}/* {store}/ 2>/dev/null || true ); "
            f"  rmdir {target} 2>/dev/null || rm -rf {target}; "
            f"  ln -s {store} {target}; "
            f"else "
            f"  ln -s {store} {target}; "
            f"fi"
        )
    return " && ".join(parts)
