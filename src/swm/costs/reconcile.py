"""Provider billing API reconciliation.

Compares local session cost totals against account-level billing data
from RunPod (``dailyCharges`` GraphQL) and Vast.ai (``/api/v1/invoices/``).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx

from swm import config as cfg
from swm.costs.db import query_sessions


def _local_spend(provider: str, since: str) -> float:
    """Sum estimated_cost + running cost for *provider* since *since*."""
    rows = query_sessions(provider=provider, since=since)
    now = datetime.now(timezone.utc)
    total = 0.0
    for r in rows:
        if r["stopped_at"] is not None and r["estimated_cost"] is not None:
            total += r["estimated_cost"]
        elif r["cost_per_hr"] is not None:
            started = datetime.fromisoformat(r["started_at"])
            total += r["cost_per_hr"] * (now - started).total_seconds() / 3600
    return round(total, 2)


# ── RunPod ───────────────────────────────────────────────────────────


def reconcile_runpod() -> dict:
    """Query RunPod ``dailyCharges`` and compare with local sessions.

    Returns a dict with ``provider_total``, ``local_total``,
    ``difference``, and ``details`` (list of daily charge dicts).
    """
    api_key = cfg.get("runpod.api_key")
    if not api_key:
        return {"error": "RunPod API key not configured"}

    query = """
    query {
        myself {
            currentSpendPerHr
            clientBalance
            dailyCharges {
                amount
                updatedAt
                podCharges
                diskCharges
                apiCharges
                serverlessCharges
                type
            }
        }
    }
    """
    with httpx.Client(timeout=30) as client:
        resp = client.post(
            "https://api.runpod.io/graphql",
            params={"api_key": str(api_key)},
            json={"query": query},
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        body = resp.json()

    myself = body.get("data", {}).get("myself", {})
    charges = myself.get("dailyCharges", [])

    # Sum pod charges from the last 30 days.
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    since_iso = cutoff.isoformat()

    provider_total = 0.0
    details: list[dict] = []
    for c in charges:
        updated = c.get("updatedAt", "")
        if updated and updated >= since_iso:
            pod_charge = c.get("podCharges") or 0
            provider_total += pod_charge
            details.append({
                "date": updated[:10],
                "pod": round(pod_charge, 2),
                "disk": round(c.get("diskCharges") or 0, 2),
                "total": round(c.get("amount") or 0, 2),
            })

    local_total = _local_spend("runpod", since_iso)

    return {
        "provider": "runpod",
        "period": "last 30 days",
        "balance": myself.get("clientBalance"),
        "current_rate": myself.get("currentSpendPerHr"),
        "provider_total": round(provider_total, 2),
        "local_total": local_total,
        "difference": round(provider_total - local_total, 2),
        "details": details,
    }


# ── Vast.ai ──────────────────────────────────────────────────────────


def reconcile_vastai() -> dict:
    """Query Vast.ai ``/api/v1/invoices/`` and compare with local sessions.

    Returns a dict with ``provider_total``, ``local_total``,
    ``difference``, and ``details``.
    """
    api_key = cfg.get("vastai.api_key")
    if not api_key:
        return {"error": "Vast.ai API key not configured"}

    now = datetime.now(timezone.utc)
    since = now - timedelta(days=30)
    filters = json.dumps({
        "when": {
            "gte": int(since.timestamp()),
            "lte": int(now.timestamp()),
        }
    })

    with httpx.Client(timeout=30) as client:
        resp = client.get(
            "https://console.vast.ai/api/v1/invoices/",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            params={"select_filters": filters, "limit": 200},
        )
        resp.raise_for_status()
        body = resp.json()

    results = body.get("results", [])

    # Sum charges (positive amounts are debits/spend).
    provider_total = 0.0
    details: list[dict] = []
    for inv in results:
        amt = inv.get("amount", 0)
        if amt > 0:
            provider_total += amt
            details.append({
                "date": datetime.fromtimestamp(
                    inv.get("start", 0), tz=timezone.utc
                ).strftime("%Y-%m-%d"),
                "type": inv.get("type", ""),
                "amount": round(amt, 2),
                "description": inv.get("description", ""),
            })

    since_iso = since.isoformat()
    local_total = _local_spend("vastai", since_iso)

    return {
        "provider": "vastai",
        "period": "last 30 days",
        "provider_total": round(provider_total, 2),
        "local_total": local_total,
        "difference": round(provider_total - local_total, 2),
        "details": details,
    }
