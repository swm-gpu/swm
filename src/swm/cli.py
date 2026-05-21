"""swm CLI entry point — registers all command groups."""
from __future__ import annotations

import click

from swm import __version__
from swm.commands._helpers import console
from swm.providers import (
    get_configured_providers,
    get_provider,
    ALL_PROVIDERS,
)

_WORKFLOW_EPILOG = """\b
Workflow:
  gpus              Search live GPU availability and pricing
  pod create        Provision a new GPU instance
  guard set         Add idle reminder / stop / down policy
  ssh <id>          SSH into a running instance
  setup install     Install frameworks (ComfyUI, SwarmUI, ...)
  setup start/stop  Start or stop installed frameworks
  models search     Find models on HuggingFace Hub
  models pull       Download a model to the pod
  models link       Register a file under the unified model store
  sync pull/push    Sync workspace with cloud storage
  costs live        Show running cost of active pods
  costs summary     Spending breakdown by provider/GPU
  pod down          Push workspace and terminate
"""


@click.group(epilog=_WORKFLOW_EPILOG)
@click.version_option(__version__, prog_name="swm")
def main():
    """swm — Cloud GPU workflow manager for ComfyUI / SwarmUI."""


# ── GPU search (top-level, kept here for brevity) ──────────────────

_SLUG_TO_NAME = {cls().slug: cls().name for cls in ALL_PROVIDERS}


def _provider_display(slug_or_name: str) -> str:
    """Map a provider slug to its human-readable name, or pass through."""
    return _SLUG_TO_NAME.get(slug_or_name, slug_or_name)


def _detect_storage_region() -> str | None:
    """Try to detect the configured storage region from B2/S3 endpoint URL."""
    import re as _re
    try:
        from swm import config as _cfg
        endpoint = _cfg.get("b2.s3_endpoint") or ""
        if endpoint:
            m = _re.search(r"s3\.([a-z0-9-]+)\.backblazeb2\.com", endpoint)
            if m:
                return m.group(1)
        endpoint = _cfg.get("s3.endpoint") or _cfg.get("s3.region") or ""
        if endpoint:
            return str(endpoint)
    except Exception:
        pass
    return None


@main.command()
@click.option("--gpu", "-g", default=None, help="Filter by GPU name (free text, e.g. h200, a100, rtx4090)")
@click.option("--count", "-c", "gpu_count", default=None, type=int, help="GPU count (e.g. 4 for 4×GPU configs)")
@click.option("--max-price", default=None, type=float, help="Max on-demand $/hr per GPU")
@click.option("--provider", "-p", default=None, help="Filter to one provider")
@click.option("--secure", is_flag=True, help="Only show secure-cloud providers")
@click.option(
    "--sort", "sort_by", default="price",
    type=click.Choice(["price", "vram", "provider"], case_sensitive=False),
    help="Sort order (default: price)",
)
@click.option("--region", "-r", default=None, help="Filter by region (free text, e.g. us-east, europe)")
@click.option("--limit", "-n", default=20, type=int, help="Max rows to show (default: 20)")
@click.option("--all", "show_all", is_flag=True, help="Show all results (no pagination)")
def gpus(gpu: str | None, gpu_count: int | None, max_price: float | None,
         provider: str | None, secure: bool, sort_by: str,
         region: str | None, limit: int, show_all: bool):
    """Search live GPU availability and pricing across all providers.

    \b
    Queries all configured providers in real-time.

    \b
    Examples:
      swm gpus                        # everything
      swm gpus -g h200                # H200s across all providers
      swm gpus -g h200 -c 4           # 4×H200 configs only
      swm gpus -g h200 --secure       # secure cloud only
      swm gpus --max-price 4          # under $4/hr
      swm gpus -p vastai              # one provider
      swm gpus -r us-west             # GPUs in US West regions
    """
    from rich.table import Table
    from swm.providers.base import GpuInfo
    from swm.cuda import min_cuda_for

    if provider:
        try:
            sources = [get_provider(provider)]
        except ValueError:
            sources = []
    else:
        configured = get_configured_providers()
        unconfigured_slugs = (
            {cls().slug for cls in ALL_PROVIDERS} - {p.slug for p in configured}
        )
        sources = list(configured)
        for slug in unconfigured_slugs:
            sources.append(get_provider(slug))

    all_gpus: list[GpuInfo] = []
    with console.status("Searching GPUs…", spinner="dots") as spin:
        for p in sources:
            label = _provider_display(p.slug) if hasattr(p, "slug") else p.name
            spin.update(f"Querying {label}…")
            try:
                results = p.list_gpus(gpu_count=gpu_count)
                all_gpus.extend(results)
                console.log(f"[green]✓[/green] {label} — {len(results)} GPUs")
            except Exception as exc:
                console.log(f"[red]✗[/red] {label} — {exc}")

    if gpu:
        needle = gpu.lower()
        all_gpus = [
            g for g in all_gpus
            if needle in g.display_name.lower() or needle in g.type_id.lower()
        ]

    if max_price is not None:
        all_gpus = [
            g for g in all_gpus
            if g.on_demand_price is not None and g.on_demand_price <= max_price
        ]

    if secure:
        all_gpus = [g for g in all_gpus if g.secure_cloud]

    if region:
        needle = region.lower()
        all_gpus = [
            g for g in all_gpus
            if any(needle in r.lower() for r in g.regions)
        ]

    if not all_gpus:
        console.print("[yellow]No GPUs found matching filters.[/yellow]")
        return

    if sort_by == "vram":
        all_gpus.sort(key=lambda g: (-g.vram_gb, g.on_demand_price or 999))
    elif sort_by == "provider":
        all_gpus.sort(key=lambda g: (_provider_display(g.provider), g.on_demand_price or 999))
    else:
        all_gpus.sort(key=lambda g: (g.on_demand_price or 999, g.gpu_count))

    total = len(all_gpus)
    truncated = False
    if not show_all and total > limit:
        all_gpus = all_gpus[:limit]
        truncated = True

    title = "GPU Availability & Pricing"
    if truncated:
        title += f"  (top {limit} of {total})"

    table = Table(title=title, title_style="bold", show_lines=True)
    table.add_column("Provider", style="bold", min_width=10)
    table.add_column("GPU")
    table.add_column("-g", style="dim cyan", no_wrap=True)
    table.add_column("VRAM", justify="right")
    table.add_column("×", justify="center")
    table.add_column("$/hr", justify="right")
    table.add_column("Spot", justify="right")
    table.add_column("Stock")
    table.add_column("Min CUDA", justify="right")
    table.add_column("Regions", max_width=30)
    table.add_column("Secure", justify="center")

    stock_styles = {
        "High": "green", "Medium": "yellow", "Low": "red",
        "None": "red bold", "available": "green", "unavailable": "red",
    }

    for g in all_gpus:
        ss = stock_styles.get(g.stock_level, "dim")
        g_flag = f'"{g.type_id}"' if " " in g.type_id else g.type_id
        regions_str = ", ".join(g.regions[:5]) if g.regions else "[dim]—[/dim]"
        if len(g.regions) > 5:
            regions_str += f" (+{len(g.regions) - 5})"
        mc = min_cuda_for(g.display_name) or min_cuda_for(g.type_id)
        mc_str = mc if mc else "[dim]—[/dim]"
        table.add_row(
            _provider_display(g.provider),
            g.display_name,
            g_flag,
            f"{g.vram_gb} GB" if g.vram_gb else "—",
            str(g.gpu_count),
            f"${g.on_demand_price:.2f}" if g.on_demand_price else "—",
            f"${g.spot_price:.2f}" if g.spot_price else "—",
            f"[{ss}]{g.stock_level or '—'}[/{ss}]",
            mc_str,
            regions_str,
            "[green]✓[/green]" if g.secure_cloud else "[dim]—[/dim]",
        )

    console.print()
    console.print(table)
    console.print()

    if truncated:
        console.print(
            f"[dim]Showing {limit} of {total} results. "
            f"Use --all to see everything or -n {total} for a specific count.[/dim]"
        )
        console.print()

    storage_region = _detect_storage_region()
    if storage_region:
        console.print(
            f"[cyan]Storage region:[/cyan] {storage_region}  "
            "[dim](GPUs in the same region will have fastest sync)[/dim]"
        )
        console.print()

    console.print(
        "[dim]Copy the [bold]-g[/bold] value into [bold]swm pod create -g <value>[/bold][/dim]"
    )
    console.print()

    gpu_hint = gpu.lower() if gpu else "<gpu>"
    count_hint = f" --gpu-count {gpu_count}" if gpu_count and gpu_count > 1 else ""
    region_hint = f" --region {storage_region}" if storage_region else ""
    cuda_hint = ""
    if all_gpus:
        cudas = {min_cuda_for(g.display_name) or min_cuda_for(g.type_id) for g in all_gpus}
        cudas.discard(None)
        if len(cudas) == 1:
            cuda_hint = f" --cuda {next(iter(cudas))}"
    console.print(
        f"[bold]Next →[/bold]  swm pod create -p <provider> -g {gpu_hint} "
        f"-n <name>{count_hint}{region_hint}{cuda_hint}"
    )
    console.print(
        "[bold]Then →[/bold]  swm setup install <framework> <provider>:<id>"
    )


# ── Register command groups ─────────────────────────────────────────

from swm.commands.config import config_group  # noqa: E402
from swm.commands.pricing import pricing  # noqa: E402
from swm.commands.pod import pod  # noqa: E402
from swm.commands.setup import setup  # noqa: E402
from swm.commands.sync import sync  # noqa: E402
from swm.commands.costs import costs  # noqa: E402
from swm.commands.storage import storage  # noqa: E402
from swm.commands.remote import ssh_connect, run, upload, download  # noqa: E402
from swm.commands.models import models_group  # noqa: E402
from swm.commands.guard import guard  # noqa: E402
from swm.commands.use import use  # noqa: E402
from swm.commands.images import images  # noqa: E402

main.add_command(config_group)
main.add_command(pricing)
main.add_command(pod)
main.add_command(setup)
main.add_command(sync)
main.add_command(costs)
main.add_command(storage)
main.add_command(ssh_connect)
main.add_command(run)
main.add_command(upload)
main.add_command(download)
main.add_command(models_group)
main.add_command(guard)
main.add_command(use)
main.add_command(images)
