"""swm pricing — compare GPU pricing across cloud providers."""
from __future__ import annotations

import click
from rich.table import Table

from swm.pricing.providers import OFFERINGS, GPU_SPECS
from swm.pricing.calculator import estimate_workload
from swm.commands._helpers import console


@click.group()
def pricing():
    """Compare GPU pricing across cloud providers."""


@pricing.command()
@click.option("--gpu", type=click.Choice(["h200", "b200"], case_sensitive=False), help="Filter by GPU type")
@click.option("--single-gpu", is_flag=True, help="Only show single-GPU providers")
def compare(gpu: str | None, single_gpu: bool):
    """Compare per-GPU/hr pricing across all providers."""
    rows = [
        o for o in OFFERINGS
        if (gpu is None or o.gpu == gpu.lower())
        and (not single_gpu or o.min_gpus == 1)
    ]
    if not rows:
        console.print("[yellow]No offerings match those filters.[/yellow]")
        return

    table = Table(
        title="GPU Pricing Comparison (per GPU / hour)",
        title_style="bold",
        show_lines=True,
    )
    table.add_column("Provider", style="bold")
    table.add_column("GPU", style="cyan")
    table.add_column("On-Demand", justify="right")
    table.add_column("Spot", justify="right")
    table.add_column("Reserved", justify="right")
    table.add_column("Min GPUs", justify="center")
    table.add_column("Stop/Resume", justify="center")
    table.add_column("Security")

    for o in sorted(rows, key=lambda x: (x.gpu, x.on_demand or 999)):
        est = "~" if o.estimated else ""
        table.add_row(
            o.provider,
            o.gpu.upper(),
            f"{est}${o.on_demand:.2f}" if o.on_demand else "[dim]—[/dim]",
            f"{est}${o.spot:.2f}" if o.spot else "[dim]—[/dim]",
            f"{est}${o.reserved:.2f}" if o.reserved else "[dim]—[/dim]",
            str(o.min_gpus),
            "[green]✓[/green]" if o.stop_resume else "[dim]✗[/dim]",
            ", ".join(o.security) if o.security else "[dim]verify[/dim]",
        )

    console.print()
    console.print(table)
    console.print()
    console.print(
        "[dim]Prices are per single GPU per hour. "
        "8-GPU node prices have been divided by 8. "
        "~ = estimated.[/dim]"
    )


@pricing.command()
@click.option("--gpu", default="h200", type=click.Choice(["h200", "b200"], case_sensitive=False), help="GPU type")
@click.option("--hours", default=3.0, type=float, help="Hours per week")
@click.option("--storage", default=100.0, type=float, help="Model storage in GB")
@click.option("--provider", default=None, help="Filter to one provider")
@click.option("--single-gpu", is_flag=True, help="Only show single-GPU providers")
@click.option("--tier", default="on_demand", type=click.Choice(["on_demand", "spot", "reserved"]), help="Pricing tier")
def estimate(gpu: str, hours: float, storage: float, provider: str | None, single_gpu: bool, tier: str):
    """Estimate monthly cost for a workload.

    Example: swm pricing estimate --gpu h200 --hours 3 --single-gpu
    """
    results = estimate_workload(
        gpu=gpu.lower(),
        hours_per_week=hours,
        storage_gb=storage,
        provider=provider,
        single_gpu_only=single_gpu,
        tier=tier,
    )
    if not results:
        console.print("[yellow]No offerings match those filters.[/yellow]")
        return

    hrs_month = hours * 4.33
    spec = GPU_SPECS[gpu.lower()]

    console.print()
    console.print(
        f"[bold]Monthly Estimate:[/bold] {spec.name}, "
        f"{hours} hrs/week ({hrs_month:.0f} hrs/month), "
        f"{storage:.0f} GB storage"
    )
    console.print()

    table = Table(show_lines=True)
    table.add_column("Provider", style="bold")
    table.add_column("Tier")
    table.add_column("$/hr (GPU)", justify="right")
    table.add_column("GPUs", justify="center")
    table.add_column("Monthly GPU", justify="right", style="cyan")
    table.add_column("Monthly Idle", justify="right")
    table.add_column("Monthly Total", justify="right", style="bold green")
    table.add_column("$/video 720p", justify="right")

    for r in results:
        if r.monthly_total_low == r.monthly_total_high:
            total_str = f"${r.monthly_total_low:,.0f}"
        else:
            total_str = f"${r.monthly_total_low:,.0f}–${r.monthly_total_high:,.0f}"

        cpv = f"${r.cpv_720p:.3f}" if r.cpv_720p else "—"

        table.add_row(
            r.offering.provider,
            r.tier_label,
            f"${r.price_used:.2f}",
            str(r.offering.min_gpus),
            f"${r.monthly_gpu:,.0f}",
            r.monthly_idle,
            total_str,
            cpv,
        )

    console.print(table)
    console.print()
    console.print(
        "[dim]$/video = estimated cost for one WAN 2.2 720p generation. "
        "Idle cost assumes stop/resume where supported.[/dim]"
    )


@pricing.command()
@click.option("--gpu", type=click.Choice(["h200", "b200"], case_sensitive=False), help="Filter by GPU type")
def specs(gpu: str | None):
    """Show GPU hardware specs side-by-side."""
    table = Table(title="GPU Specifications", title_style="bold", show_lines=True)
    table.add_column("Spec", style="bold")

    targets = {gpu.lower(): GPU_SPECS[gpu.lower()]} if gpu else GPU_SPECS
    for key in targets:
        table.add_column(targets[key].name, justify="right", style="cyan")

    rows = [
        ("VRAM", lambda s: f"{s.vram_gb} GB HBM3e"),
        ("Memory Bandwidth", lambda s: f"{s.mem_bandwidth_tbs} TB/s"),
        ("BF16 TFLOPS", lambda s: f"{s.bf16_tflops:,}"),
        ("FP8 TFLOPS", lambda s: f"{s.fp8_tflops:,}"),
        ("Est. 720p gen time", lambda s: f"~{s.generation_seconds.get('720p', '?')}s"),
    ]
    for label, fn in rows:
        table.add_row(label, *[fn(s) for s in targets.values()])

    console.print()
    console.print(table)
