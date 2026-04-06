"""High-level cost tracking functions used by CLI hooks.

Every function here is safe to call in a best-effort manner — callers
should wrap calls in ``try/except`` so that a database error never
blocks a pod operation.
"""

from __future__ import annotations

from datetime import datetime, timezone

from swm.costs.db import (
    active_sessions,
    end_session,
    insert_session,
)
from swm.providers.base import Instance


def record_start(
    inst: Instance,
    workspace: str | None = None,
) -> int:
    """Open a new billing session for *inst*.

    If ``inst.cost_per_hr`` is ``None``, attempts a rate lookup from the
    provider's ``list_gpus()`` before recording.
    """
    rate = inst.cost_per_hr
    if rate is None:
        rate = _lookup_rate(inst.provider, inst.gpu_type, inst.gpu_count)

    return insert_session(
        pod_id=inst.id,
        provider=inst.provider,
        gpu_type=inst.gpu_type,
        gpu_count=inst.gpu_count,
        cost_per_hr=rate,
        workspace=workspace,
        name=inst.name,
    )


def record_stop(pod_id: str, provider: str) -> bool:
    """Close the active billing session for *pod_id*. Idempotent."""
    return end_session(pod_id, provider)


def live_cost(pod_id: str | None = None) -> list[dict]:
    """Return running cost info for active sessions.

    Each dict has keys: pod_id, provider, gpu_type, gpu_count, name,
    cost_per_hr, started_at, elapsed_hrs, running_cost.

    If *pod_id* is given, returns only matching sessions.
    """
    now = datetime.now(timezone.utc)
    results: list[dict] = []
    for row in active_sessions():
        if pod_id and row["pod_id"] != pod_id:
            continue
        started = datetime.fromisoformat(row["started_at"])
        elapsed_hrs = (now - started).total_seconds() / 3600
        rate = row["cost_per_hr"]
        results.append({
            "pod_id": row["pod_id"],
            "provider": row["provider"],
            "gpu_type": row["gpu_type"],
            "gpu_count": row["gpu_count"],
            "name": row["name"],
            "cost_per_hr": rate,
            "started_at": row["started_at"],
            "elapsed_hrs": round(elapsed_hrs, 3),
            "running_cost": round(rate * elapsed_hrs, 4) if rate else None,
        })
    return results


def _lookup_rate(
    provider_slug: str, gpu_type: str | None, gpu_count: int,
) -> float | None:
    """Try to resolve a per-hour rate from provider GPU listings."""
    if not gpu_type:
        return None
    try:
        from swm.providers import get_provider

        prov = get_provider(provider_slug)
        gpus = prov.list_gpus(gpu_count=gpu_count)
        needle = gpu_type.lower()
        for g in gpus:
            if g.type_id.lower() == needle or g.display_name.lower() == needle:
                return g.on_demand_price
    except Exception:
        pass
    return None
