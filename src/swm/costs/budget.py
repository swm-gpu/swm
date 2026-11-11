"""Budget checking and management.

Budget warnings are advisory only — they never block pod operations.
"""

from __future__ import annotations

from swm.costs.db import (
    get_budgets,
    parse_pod_scope,
    set_budget as _db_set_budget,
    spend_in_period,
)


def set_budget(scope: str, limit_usd: float, period: str) -> int:
    """Create or replace a budget. Returns the row id."""
    return _db_set_budget(scope, limit_usd, period)


def _applies_to(scope: str, provider: str, pod_id: str | None) -> bool:
    """Whether *scope* is a budget that applies to this provider / pod."""
    if scope == "global" or scope == f"provider:{provider}":
        return True
    pod = parse_pod_scope(scope)
    if not pod_id or pod is None:
        return False
    scoped_provider, scoped_pod = pod
    return scoped_pod == pod_id and scoped_provider in (None, provider)


def check_budget(provider: str, pod_id: str | None = None) -> str | None:
    """Check all applicable budgets and return a warning string, or None.

    Checks budgets matching ``global``, ``provider:<slug>``, and — when
    *pod_id* is given — the budget scoped to that pod.  Returns a formatted
    warning if spend is at or above 80 % of any limit.
    """
    budgets = get_budgets()
    if not budgets:
        return None

    warnings: list[str] = []
    for b in budgets:
        scope: str = b["scope"]
        if not _applies_to(scope, provider, pod_id):
            continue
        spent = spend_in_period(scope, b["period"])
        limit = b["limit_usd"]
        pct = (spent / limit * 100) if limit > 0 else 0

        if pct >= 100:
            warnings.append(
                f"OVER BUDGET ({scope}, {b['period']}): "
                f"${spent:.2f} / ${limit:.2f} ({pct:.0f}%)"
            )
        elif pct >= 80:
            warnings.append(
                f"Approaching budget ({scope}, {b['period']}): "
                f"${spent:.2f} / ${limit:.2f} ({pct:.0f}%)"
            )

    if not warnings:
        return None

    return "\n".join(warnings)


def budget_status() -> list[dict]:
    """Return a summary of each budget with current spend.

    Each dict has: scope, period, limit_usd, spent, pct.
    """
    results: list[dict] = []
    for b in get_budgets():
        spent = spend_in_period(b["scope"], b["period"])
        limit = b["limit_usd"]
        results.append({
            "scope": b["scope"],
            "period": b["period"],
            "limit_usd": limit,
            "spent": round(spent, 2),
            "pct": round(spent / limit * 100, 1) if limit > 0 else 0,
        })
    return results
