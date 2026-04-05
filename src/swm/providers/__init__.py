from __future__ import annotations

from swm.providers.base import (
    CloudProvider,
    CreateConfig,
    GpuInfo,
    Instance,
    InstanceStatus,
)
from swm.providers.runpod import RunPodProvider
from swm.providers.aws import AWSProvider
from swm.providers.gcp import GCPProvider
from swm.providers.coreweave import CoreWeaveProvider
from swm.providers.vastai import VastAIProvider
from swm.providers.lambda_labs import LambdaLabsProvider

ALL_PROVIDERS: list[type[CloudProvider]] = [
    RunPodProvider,
    AWSProvider,
    GCPProvider,
    CoreWeaveProvider,
    VastAIProvider,
    LambdaLabsProvider,
]

PROVIDER_SLUGS = {cls().slug: cls for cls in ALL_PROVIDERS}


def get_provider(slug: str) -> CloudProvider:
    cls = PROVIDER_SLUGS.get(slug)
    if cls is None:
        raise ValueError(
            f"Unknown provider '{slug}'. "
            f"Available: {', '.join(PROVIDER_SLUGS)}"
        )
    return cls()


def get_configured_providers() -> list[CloudProvider]:
    """Return only providers that have valid credentials."""
    result: list[CloudProvider] = []
    for cls in ALL_PROVIDERS:
        p = cls()
        try:
            if p.is_configured():
                result.append(p)
        except Exception:
            pass
    return result


def resolve_instance(
    instance_id: str,
) -> tuple[CloudProvider, str]:
    """Resolve 'provider:id' or bare 'id' to (provider, raw_id).

    For bare IDs, queries all configured providers to locate the instance.
    """
    if ":" in instance_id:
        slug, raw = instance_id.split(":", 1)
        return get_provider(slug), raw

    for provider in get_configured_providers():
        try:
            for inst in provider.list_instances():
                if inst.id == instance_id or inst.name == instance_id:
                    return provider, inst.id
        except Exception:
            continue

    raise ValueError(
        f"Instance '{instance_id}' not found. "
        "Use 'provider:id' format or configure providers first."
    )


__all__ = [
    "CloudProvider",
    "CreateConfig",
    "GpuInfo",
    "Instance",
    "InstanceStatus",
    "RunPodProvider",
    "AWSProvider",
    "GCPProvider",
    "CoreWeaveProvider",
    "VastAIProvider",
    "LambdaLabsProvider",
    "ALL_PROVIDERS",
    "PROVIDER_SLUGS",
    "get_provider",
    "get_configured_providers",
    "resolve_instance",
]
