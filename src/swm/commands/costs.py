"""swm costs — track GPU usage and spending."""
from __future__ import annotations

from datetime import datetime, timezone

import click
from rich.table import Table

from swm.commands._helpers import console


def _session_cost(row, now) -> float | None:
    """Compute cost for a session row (closed or running)."""
    if row["stopped_at"] and row["estimated_cost"] is not None:
        return row["estimated_cost"]
    if row["cost_per_hr"] is not None:
        started = datetime.fromisoformat(row["started_at"])
        end = datetime.fromisoformat(row["stopped_at"]) if row["stopped_at"] else now
        return round(row["cost_per_hr"] * (end - started).total_seconds() / 3600, 4)
    return None


@click.group()
def costs():
    """Track GPU usage and spending."""


@costs.command()
@click.option(
    "--period", "-t",
    type=click.Choice(["today", "week", "month", "all"], case_sensitive=False),
    default="month",
    help="Time window (default: month)",
)
@click.option("--provider", "-p", default=None, help="Filter to one provider")
def summary(period: str, provider: str | None):
    """Show spending summary grouped by provider and GPU type."""
    from datetime import timedelta
    from collections import defaultdict
    from swm.costs.db import query_sessions

    now = datetime.now(timezone.utc)
    since: str | None = None
    label = period
    if period == "today":
        since = (now - timedelta(days=1)).isoformat()
    elif period == "week":
        since = (now - timedelta(weeks=1)).isoformat()
    elif period == "month":
        since = (now - timedelta(days=30)).isoformat()
    else:
        label = "all time"

    rows = query_sessions(provider=provider, since=since)

    if not rows:
        console.print(f"[dim]No sessions recorded for {label}.[/dim]")
        return

    by_provider: dict[str, float] = defaultdict(float)
    by_gpu: dict[str, float] = defaultdict(float)
    total_hrs = 0.0
    total_cost = 0.0
    unpriced = 0

    for r in rows:
        cost = _session_cost(r, now)
        started = datetime.fromisoformat(r["started_at"])
        end = datetime.fromisoformat(r["stopped_at"]) if r["stopped_at"] else now
        hrs = (end - started).total_seconds() / 3600

        if cost is not None:
            by_provider[r["provider"]] += cost
            by_gpu[r["gpu_type"] or "unknown"] += cost
            total_cost += cost
        else:
            unpriced += 1
        total_hrs += hrs

    table = Table(title=f"Spending summary ({label})", show_footer=True)
    table.add_column("Provider", footer="TOTAL")
    table.add_column("GPU Hours", justify="right", footer=f"{total_hrs:.1f}")
    table.add_column("Cost", justify="right", footer=f"${total_cost:.2f}")

    for prov, cost in sorted(by_provider.items()):
        prov_hrs = sum(
            (datetime.fromisoformat(r["stopped_at"] or now.isoformat())
             - datetime.fromisoformat(r["started_at"])).total_seconds() / 3600
            for r in rows if r["provider"] == prov
        )
        table.add_row(prov, f"{prov_hrs:.1f}", f"${cost:.2f}")

    console.print()
    console.print(table)

    if by_gpu:
        gpu_table = Table(title="By GPU type")
        gpu_table.add_column("GPU")
        gpu_table.add_column("Cost", justify="right")
        for gpu, cost in sorted(by_gpu.items(), key=lambda x: -x[1]):
            gpu_table.add_row(gpu, f"${cost:.2f}")
        console.print()
        console.print(gpu_table)

    if unpriced:
        console.print(f"\n  [dim]{unpriced} session(s) with unknown rate — not included in cost totals[/dim]")


@costs.command()
@click.option("--limit", "-n", default=20, type=int, help="Number of sessions to show")
@click.option("--provider", "-p", default=None, help="Filter to one provider")
def log(limit: int, provider: str | None):
    """Show detailed session log."""
    from swm.costs.db import query_sessions

    rows = query_sessions(provider=provider, limit=limit)

    if not rows:
        console.print("[dim]No sessions recorded.[/dim]")
        return

    now = datetime.now(timezone.utc)
    table = Table(title=f"Session log (last {limit})")
    table.add_column("Pod")
    table.add_column("Provider")
    table.add_column("GPU")
    table.add_column("Started", no_wrap=True)
    table.add_column("Duration", justify="right")
    table.add_column("$/hr", justify="right")
    table.add_column("Cost", justify="right")
    table.add_column("Status")

    for r in rows:
        started = datetime.fromisoformat(r["started_at"])
        end = datetime.fromisoformat(r["stopped_at"]) if r["stopped_at"] else now
        dur = end - started
        hours = dur.total_seconds() / 3600
        dur_str = f"{int(hours)}h {int(dur.total_seconds() % 3600 / 60)}m"
        cost = _session_cost(r, now)
        cost_str = f"${cost:.2f}" if cost is not None else "—"
        rate_str = f"${r['cost_per_hr']:.2f}" if r["cost_per_hr"] else "—"
        gpu_str = f"{r['gpu_type'] or '?'} ×{r['gpu_count']}"
        status = "[green]●[/green] running" if not r["stopped_at"] else "[dim]stopped[/dim]"

        table.add_row(
            r["name"] or r["pod_id"][:12],
            r["provider"],
            gpu_str,
            started.strftime("%Y-%m-%d %H:%M"),
            dur_str,
            rate_str,
            cost_str,
            status,
        )

    console.print()
    console.print(table)


@costs.command()
def live():
    """Show running cost of active pods."""
    from swm.costs.tracker import live_cost

    sessions = live_cost()
    if not sessions:
        console.print("[dim]No active billing sessions.[/dim]")
        return

    table = Table(title="Active sessions — live cost")
    table.add_column("Pod")
    table.add_column("Provider")
    table.add_column("GPU")
    table.add_column("Elapsed", justify="right")
    table.add_column("$/hr", justify="right")
    table.add_column("Running cost", justify="right")

    for s in sessions:
        hrs = s["elapsed_hrs"]
        elapsed_str = f"{int(hrs)}h {int((hrs % 1) * 60)}m"
        rate_str = f"${s['cost_per_hr']:.2f}" if s["cost_per_hr"] else "—"
        cost_str = f"${s['running_cost']:.2f}" if s["running_cost"] is not None else "—"
        gpu_str = f"{s['gpu_type'] or '?'} ×{s['gpu_count']}"

        table.add_row(
            s["name"] or s["pod_id"][:12],
            s["provider"],
            gpu_str,
            elapsed_str,
            rate_str,
            cost_str,
        )

    console.print()
    console.print(table)


@costs.group()
def budget():
    """Manage spending budgets."""


@budget.command(name="set")
@click.argument("amount", type=float)
@click.option(
    "--scope", "-s", default="global",
    help="Budget scope: global, provider:<slug>, or pod:<id>",
)
@click.option(
    "--period", "-t",
    type=click.Choice(["daily", "weekly", "monthly", "total"], case_sensitive=False),
    default="monthly",
    help="Budget period (default: monthly)",
)
def budget_set(amount: float, scope: str, period: str):
    """Set a spending budget.

    \b
    Examples:
      swm costs budget set 50                        # $50/month global
      swm costs budget set 100 --scope provider:runpod
      swm costs budget set 10 --period daily
    """
    from swm.costs.budget import set_budget

    set_budget(scope, amount, period)
    console.print(
        f"[green]✓[/green] Budget set: ${amount:.2f}/{period} "
        f"(scope: {scope})"
    )


@budget.command(name="show")
def budget_show():
    """Show active budgets with current spend."""
    from swm.costs.budget import budget_status

    budgets = budget_status()
    if not budgets:
        console.print("[dim]No budgets configured. Set one: swm costs budget set <amount>[/dim]")
        return

    table = Table(title="Budgets")
    table.add_column("Scope")
    table.add_column("Period")
    table.add_column("Limit", justify="right")
    table.add_column("Spent", justify="right")
    table.add_column("Used")

    for b in budgets:
        pct = b["pct"]
        if pct >= 100:
            color = "red"
        elif pct >= 80:
            color = "yellow"
        else:
            color = "green"
        bar = f"[{color}]{'█' * int(pct / 5)}{'░' * (20 - int(pct / 5))}[/{color}] {pct:.0f}%"

        table.add_row(
            b["scope"],
            b["period"],
            f"${b['limit_usd']:.2f}",
            f"${b['spent']:.2f}",
            bar,
        )

    console.print()
    console.print(table)


@budget.command(name="remove")
@click.argument("scope")
@click.option(
    "--period", "-t",
    type=click.Choice(["daily", "weekly", "monthly", "total"], case_sensitive=False),
    default="monthly",
)
def budget_remove(scope: str, period: str):
    """Remove a budget. Example: swm costs budget remove global"""
    from swm.costs.db import delete_budget

    if delete_budget(scope, period):
        console.print(f"[green]✓[/green] Budget removed: {scope}/{period}")
    else:
        console.print(f"[yellow]No budget found for {scope}/{period}[/yellow]")


def _render_report(report) -> None:
    """Render a :class:`BillingReport` to the console."""
    from swm.costs.billing import BillingReport

    r: BillingReport = report

    console.print(f"\n[bold]{r.provider}[/bold] — {r.period}")

    # --- account overview ---
    parts: list[str] = []
    if r.balance is not None:
        parts.append(f"  Balance:        ${r.balance:.2f}")
    if r.current_rate_hr is not None:
        rate_str = f"${r.current_rate_hr:.4f}/hr"
        if r.rate_breakdown:
            detail = ", ".join(f"{k}: ${v:.2f}" for k, v in r.rate_breakdown.items())
            rate_str += f"  ({detail})"
        parts.append(f"  Spend rate:     {rate_str}")
    if r.lifetime_spend is not None:
        parts.append(f"  Lifetime spend: ${r.lifetime_spend:,.2f}")
    if parts:
        console.print("\n".join(parts))

    # --- card / payment charges ---
    if r.payments:
        console.print()
        pay_table = Table(title="Card charges")
        pay_table.add_column("Date", no_wrap=True)
        pay_table.add_column("Method")
        pay_table.add_column("Description")
        pay_table.add_column("Count", justify="right")
        pay_table.add_column("Amount", justify="right")
        has_receipt = any(p.receipt_url for p in r.payments)
        if has_receipt:
            pay_table.add_column("Receipt")

        grouped: dict[tuple, dict] = {}
        for p in r.payments:
            key = (p.date, p.method, p.description)
            if key not in grouped:
                grouped[key] = {"amount": 0.0, "count": 0, "receipt": p.receipt_url}
            grouped[key]["amount"] += p.amount
            grouped[key]["count"] += 1

        for (date, method, desc), g in sorted(grouped.items(), reverse=True):
            row = [date, method, desc, str(g["count"]), f"${g['amount']:.2f}"]
            if has_receipt:
                row.append("[link]receipt[/link]" if g["receipt"] else "—")
            pay_table.add_row(*row)

        console.print(pay_table)
        console.print(f"  Total charged: [bold]${r.payment_total:.2f}[/bold]")
    else:
        console.print("\n  [dim]No card charges found for this period.[/dim]")

    # --- usage breakdown ---
    if r.usage:
        console.print()
        use_table = Table(title="Usage")
        use_table.add_column("Resource")
        has_hours = any(u.hours is not None for u in r.usage)
        if has_hours:
            use_table.add_column("Hours", justify="right")
        use_table.add_column("Cost", justify="right")

        for u in r.usage:
            row = [u.resource]
            if has_hours:
                row.append(f"{u.hours:.1f}h" if u.hours is not None else "—")
            row.append(f"${u.amount:.2f}")
            use_table.add_row(*row)

        console.print(use_table)
        console.print(f"  Provider usage total: [bold]${r.usage_total:.2f}[/bold]")
    else:
        console.print("\n  [dim]No usage records found for this period.[/dim]")

    # --- reconciliation vs local ---
    console.print()
    rec_table = Table(title="Reconciliation")
    rec_table.add_column("Source", min_width=20)
    rec_table.add_column("Total", justify="right")

    rec_table.add_row("Charged to card", f"${r.payment_total:.2f}")
    if r.usage:
        rec_table.add_row("Provider (usage)", f"${r.usage_total:.2f}")
    rec_table.add_row("Local (swm)", f"${r.local_total:.2f}")

    compare_total = r.usage_total if r.usage else r.payment_total
    diff = round(compare_total - r.local_total, 2)
    diff_color = "green" if abs(diff) < 1 else "yellow"
    rec_table.add_row(
        "Difference",
        f"[{diff_color}]${diff:+.2f}[/{diff_color}]",
    )
    console.print(rec_table)

    if not r.usage and r.payments:
        console.print(
            "  [dim]This provider does not expose per-resource usage; "
            "comparing card charges vs local tracking.[/dim]"
        )


@costs.command()
@click.option("--provider", "-p", default=None,
              type=click.Choice(["runpod", "vastai"], case_sensitive=False),
              help="Reconcile one provider only")
def reconcile(provider: str | None):
    """Show card charges, usage costs, and compare with local tracking.

    \b
    Queries provider billing APIs for:
      - Card / payment charges (Stripe auto-reload, crypto, etc.)
      - GPU compute and storage usage costs
      - Account balance and spend rate
    Then compares provider-reported usage against swm's local records.
    """
    from swm.costs.reconcile import reconcile_runpod, reconcile_vastai

    targets = []
    if provider is None or provider == "runpod":
        targets.append(("RunPod", reconcile_runpod))
    if provider is None or provider == "vastai":
        targets.append(("Vast.ai", reconcile_vastai))

    for name, fn in targets:
        with console.status(f"Querying {name} billing API…", spinner="dots"):
            try:
                report = fn()
            except Exception as e:
                console.print(f"\n  [yellow]{name}: {e}[/yellow]")
                continue
        _render_report(report)
