from __future__ import annotations

from dataclasses import dataclass

from swm.pricing.providers import OFFERINGS, GPU_SPECS, GpuOffering

WEEKS_PER_MONTH = 4.33


def monthly_cost(price_per_hour: float, hours_per_week: float) -> float:
    return price_per_hour * hours_per_week * WEEKS_PER_MONTH


def cost_per_video(
    price_per_hour: float,
    gpu: str,
    resolution: str = "720p",
) -> float | None:
    spec = GPU_SPECS.get(gpu)
    if spec is None:
        return None
    seconds = spec.generation_seconds.get(resolution)
    if seconds is None:
        return None
    return price_per_hour * seconds / 3600


@dataclass
class WorkloadEstimate:
    offering: GpuOffering
    price_used: float
    tier_label: str
    monthly_gpu: float
    monthly_idle: str
    monthly_total_low: float
    monthly_total_high: float
    cpv_480p: float | None
    cpv_720p: float | None
    cpv_1080p: float | None


def _idle_range(offering: GpuOffering, storage_gb: float) -> tuple[float, float, str]:
    """Return (low, high, description) for idle storage cost per month."""
    if not offering.stop_resume:
        return 0.0, 0.0, "N/A (no stop/resume)"
    if "runpod" in offering.provider.lower():
        cost = storage_gb * 0.20
        return cost, cost, f"~${cost:.0f} ({storage_gb:.0f} GB × $0.20)"
    if "gcp" in offering.provider.lower():
        low = storage_gb * 0.04
        high = storage_gb * 0.17
        return low, high, f"${low:.0f}–${high:.0f}"
    if "aws" in offering.provider.lower():
        cost = storage_gb * 0.08
        return cost, cost, f"~${cost:.0f}"
    if "azure" in offering.provider.lower():
        low = storage_gb * 0.05
        high = storage_gb * 0.15
        return low, high, f"${low:.0f}–${high:.0f}"
    return 0.0, 0.0, "—"


def estimate_workload(
    gpu: str,
    hours_per_week: float,
    storage_gb: float = 100.0,
    provider: str | None = None,
    single_gpu_only: bool = False,
    tier: str = "on_demand",
) -> list[WorkloadEstimate]:
    results: list[WorkloadEstimate] = []

    for o in OFFERINGS:
        if o.gpu != gpu:
            continue
        if provider and o.provider.lower() != provider.lower():
            continue
        if single_gpu_only and o.min_gpus > 1:
            continue

        if tier == "spot" and o.spot is not None:
            price = o.spot
            label = "Spot"
        elif tier == "reserved" and o.reserved is not None:
            price = o.reserved
            label = "Reserved"
        elif o.on_demand is not None:
            price = o.on_demand
            label = "On-Demand"
        else:
            continue

        effective_price = price * o.min_gpus if o.min_gpus > 1 else price
        gpu_monthly = monthly_cost(effective_price, hours_per_week)

        idle_low, idle_high, idle_desc = _idle_range(o, storage_gb)

        results.append(
            WorkloadEstimate(
                offering=o,
                price_used=price,
                tier_label=label,
                monthly_gpu=gpu_monthly,
                monthly_idle=idle_desc,
                monthly_total_low=gpu_monthly + idle_low,
                monthly_total_high=gpu_monthly + idle_high,
                cpv_480p=cost_per_video(price, gpu, "480p"),
                cpv_720p=cost_per_video(price, gpu, "720p"),
                cpv_1080p=cost_per_video(price, gpu, "1080p"),
            )
        )

    results.sort(key=lambda r: r.monthly_total_low)
    return results
