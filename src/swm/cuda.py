"""GPU → minimum CUDA toolkit version mapping.

Compute capability of an NVIDIA architecture is fixed in silicon, so this
table only changes when a new generation ships. Used to hint users about
which CUDA toolkit (image) is required for a given GPU when they run
`swm gpus` or `swm pod create --cuda <X.Y>`.
"""
from __future__ import annotations

import re

# Ordered most-specific → least-specific so the first substring hit wins.
# Each entry is (substring_to_match_lowercase, min_cuda_string).
_GPU_MIN_CUDA: tuple[tuple[str, str], ...] = (
    # Blackwell (compute 10.0 / 12.0)
    ("b300", "12.8"),
    ("b200", "12.8"),
    ("b100", "12.8"),
    ("gb200", "12.8"),
    ("rtx 5090", "12.8"),
    ("rtx 5080", "12.8"),
    ("rtx 5070", "12.8"),
    ("rtx 5060", "12.8"),
    ("rtx 5050", "12.8"),
    ("rtx pro 6000 blackwell", "12.8"),
    ("blackwell", "12.8"),
    # Hopper (compute 9.0)
    ("gh200", "11.8"),
    ("h200", "11.8"),
    ("h100", "11.8"),
    ("hopper", "11.8"),
    # Ada Lovelace (compute 8.9)
    ("l40s", "11.8"),
    ("l40", "11.8"),
    ("l4", "11.8"),
    ("rtx 6000 ada", "11.8"),
    ("rtx 4090", "11.8"),
    ("rtx 4080", "11.8"),
    ("ada", "11.8"),
    # Ampere (compute 8.0 / 8.6)
    ("a100", "11.0"),
    ("a40", "11.0"),
    ("a6000", "11.0"),
    ("a5000", "11.0"),
    ("a4500", "11.0"),
    ("a4000", "11.0"),
    ("a30", "11.0"),
    ("a10", "11.0"),
    ("rtx 3090", "11.0"),
    ("rtx 3080", "11.0"),
    ("ampere", "11.0"),
    # Turing (compute 7.5)
    ("t4", "10.0"),
    ("rtx 2080", "10.0"),
    ("turing", "10.0"),
    # Volta (compute 7.0)
    ("v100", "10.0"),
    ("volta", "10.0"),
)


def min_cuda_for(gpu_name: str | None) -> str | None:
    """Return the minimum CUDA toolkit version for a GPU display name, or None.

    Matching is substring-based on a lowercased name. The table is ordered
    most-specific first so e.g. "RTX 6000 Ada" hits before a generic "Ada".
    """
    if not gpu_name:
        return None
    needle = gpu_name.lower()
    for sub, ver in _GPU_MIN_CUDA:
        if sub in needle:
            return ver
    return None


_VER_RE = re.compile(r"^(\d+)\.(\d+)")


def cuda_at_least(image_cuda: str | None, required: str | None) -> bool:
    """Return True iff image_cuda >= required (semver-style major.minor compare).

    Returns True if either side is None (we don't have enough info to flag).
    """
    if not image_cuda or not required:
        return True
    a = _VER_RE.match(image_cuda)
    b = _VER_RE.match(required)
    if not a or not b:
        return True
    return (int(a.group(1)), int(a.group(2))) >= (int(b.group(1)), int(b.group(2)))
