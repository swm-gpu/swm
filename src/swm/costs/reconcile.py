"""Provider billing API reconciliation.

Each ``reconcile_*`` function queries the provider's billing / payment
APIs and returns a generic :class:`BillingReport`.  The CLI renders the
report uniformly regardless of the underlying provider.

Supported providers
-------------------
* **RunPod** — REST ``/billing/pods`` for usage, GraphQL
  ``stripeReloadHistory`` for card charges.
* **Vast.ai** — ``/api/v1/invoices/`` filtered by *service* for card
  charges vs. usage line-items.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import httpx

from swm import config as cfg
from swm.costs.billing import BillingReport, PaymentRecord, UsageRecord
from swm.costs.db import query_sessions

log = logging.getLogger(__name__)

_PERIOD_DAYS = 30


# ── helpers ───────────────────────────────────────────────────────────


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


def _time_window() -> tuple[datetime, datetime, str]:
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=_PERIOD_DAYS)
    return since, now, since.isoformat()


# ── RunPod ────────────────────────────────────────────────────────────

_RUNPOD_GQL = "https://api.runpod.io/graphql"
_RUNPOD_REST = "https://rest.runpod.io/v1"


def _runpod_account(client: httpx.Client, api_key: str) -> dict:
    """Fetch account-level info + stripe reload history via GraphQL."""
    query = """
    query {
        myself {
            clientBalance
            currentSpendPerHr
            clientLifetimeSpend
            spendDetails {
                localStoragePerHour
                networkStoragePerHour
                gpuComputePerHour
            }
            stripeReloadHistory {
                id
                amount
                transactionCompletedAt
                receiptLink
                medium
                type
            }
        }
    }
    """
    resp = client.post(
        _RUNPOD_GQL,
        params={"api_key": api_key},
        json={"query": query},
        headers={"Content-Type": "application/json"},
    )
    resp.raise_for_status()
    return resp.json().get("data", {}).get("myself", {})


def _runpod_pod_billing(
    client: httpx.Client,
    api_key: str,
    since: datetime,
    now: datetime,
) -> list[dict]:
    """Fetch pod billing via the REST API, grouped by GPU type."""
    resp = client.get(
        f"{_RUNPOD_REST}/billing/pods",
        headers={"Authorization": f"Bearer {api_key}"},
        params={
            "bucketSize": "day",
            "startTime": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endTime": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "grouping": "gpuTypeId",
        },
    )
    resp.raise_for_status()
    return resp.json() if isinstance(resp.json(), list) else []


def reconcile_runpod() -> BillingReport:
    """Build a :class:`BillingReport` for RunPod."""
    api_key = cfg.get("runpod.api_key")
    if not api_key:
        raise ValueError("RunPod API key not configured")

    since, now, since_iso = _time_window()

    with httpx.Client(timeout=30) as client:
        myself = _runpod_account(client, str(api_key))
        pod_records = _runpod_pod_billing(client, str(api_key), since, now)

    # --- account overview ---
    spend = myself.get("spendDetails") or {}
    rate_breakdown: dict[str, float] = {}
    if spend.get("gpuComputePerHour"):
        rate_breakdown["GPU compute"] = spend["gpuComputePerHour"]
    if spend.get("networkStoragePerHour"):
        rate_breakdown["Network storage"] = spend["networkStoragePerHour"]
    if spend.get("localStoragePerHour"):
        rate_breakdown["Local storage"] = spend["localStoragePerHour"]

    # --- card charges (stripe reload history) ---
    payments: list[PaymentRecord] = []
    for tx in myself.get("stripeReloadHistory") or []:
        completed = tx.get("transactionCompletedAt") or ""
        if completed and completed >= since_iso:
            payments.append(PaymentRecord(
                date=completed[:10],
                amount=tx.get("amount") or 0,
                method=tx.get("medium") or "Stripe",
                description=f"{tx.get('type', 'Reload')} via {tx.get('medium', 'Stripe')}",
                receipt_url=tx.get("receiptLink"),
            ))

    # --- pod usage from REST billing ---
    gpu_totals: dict[str, dict] = defaultdict(lambda: {"hours": 0.0, "amount": 0.0})
    for rec in pod_records:
        gpu = rec.get("gpuTypeId") or "Unknown"
        gpu_totals[gpu]["amount"] += rec.get("amount") or 0
        ms = rec.get("timeBilledMs") or 0
        gpu_totals[gpu]["hours"] += ms / 3_600_000

    usage: list[UsageRecord] = []
    for gpu, totals in sorted(gpu_totals.items()):
        usage.append(UsageRecord(
            period=f"last {_PERIOD_DAYS} days",
            resource=gpu,
            hours=round(totals["hours"], 1),
            amount=round(totals["amount"], 2),
        ))

    return BillingReport(
        provider="runpod",
        period=f"last {_PERIOD_DAYS} days",
        balance=myself.get("clientBalance"),
        current_rate_hr=myself.get("currentSpendPerHr"),
        lifetime_spend=myself.get("clientLifetimeSpend"),
        rate_breakdown=rate_breakdown,
        payments=payments,
        usage=usage,
        local_total=_local_spend("runpod", since_iso),
    )


# ── Vast.ai ──────────────────────────────────────────────────────────

_VASTAI_BASE = "https://console.vast.ai"
_PAYMENT_SERVICES = frozenset({
    "stripe_payments", "bitpay", "coinbase",
    "crypto.com", "paypal_manual", "wise_manual",
})


def _vastai_invoices(
    client: httpx.Client,
    api_key: str,
    since: datetime,
    now: datetime,
    service_filter: dict | None = None,
) -> list[dict]:
    """Query Vast.ai invoices endpoint with optional service filter."""
    filters: dict = {
        "when": {
            "gte": int(since.timestamp()),
            "lte": int(now.timestamp()),
        },
    }
    if service_filter:
        filters["service"] = service_filter

    resp = client.get(
        f"{_VASTAI_BASE}/api/v1/invoices/",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        params={
            "select_filters": json.dumps(filters),
            "limit": 200,
        },
    )
    resp.raise_for_status()
    body = resp.json()
    return body.get("results") or []


def reconcile_vastai() -> BillingReport:
    """Build a :class:`BillingReport` for Vast.ai."""
    api_key = cfg.get("vastai.api_key")
    if not api_key:
        raise ValueError("Vast.ai API key not configured")

    since, now, since_iso = _time_window()

    with httpx.Client(timeout=30) as client:
        payment_rows = _vastai_invoices(
            client, str(api_key), since, now,
            service_filter={"in": list(_PAYMENT_SERVICES)},
        )
        all_rows = _vastai_invoices(client, str(api_key), since, now)

    # --- card charges (negative amounts = money paid in) ---
    payments: list[PaymentRecord] = []
    for inv in payment_rows:
        amt = inv.get("amount", 0)
        meta = inv.get("metadata") or {}
        service = meta.get("service") or inv.get("type", "")
        last4 = meta.get("last4")
        network = meta.get("network")

        desc = inv.get("description") or service
        if last4:
            card_label = f"{network.upper()} ending {last4}" if network else f"Card ending {last4}"
            desc = card_label

        payments.append(PaymentRecord(
            date=datetime.fromtimestamp(
                inv.get("start") or inv.get("end") or 0, tz=timezone.utc,
            ).strftime("%Y-%m-%d"),
            amount=round(abs(amt), 2),
            method=service.replace("_", " ").title() if service else "Unknown",
            description=desc,
        ))

    # --- usage (everything that isn't a payment top-up) ---
    usage_by_type: dict[str, float] = defaultdict(float)
    for inv in all_rows:
        meta = inv.get("metadata") or {}
        service = meta.get("service") or ""
        if service in _PAYMENT_SERVICES:
            continue
        amt = inv.get("amount", 0)
        if amt > 0:
            label = inv.get("type") or service or "other"
            usage_by_type[label] += amt

    usage: list[UsageRecord] = []
    for label, total in sorted(usage_by_type.items()):
        usage.append(UsageRecord(
            period=f"last {_PERIOD_DAYS} days",
            resource=label,
            amount=round(total, 2),
        ))

    return BillingReport(
        provider="vastai",
        period=f"last {_PERIOD_DAYS} days",
        payments=payments,
        usage=usage,
        local_total=_local_spend("vastai", since_iso),
    )
