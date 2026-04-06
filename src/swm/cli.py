from __future__ import annotations

import os

import click
from rich.console import Console
from rich.table import Table

from swm import __version__
from swm import config as cfg
from swm.pricing.providers import OFFERINGS, GPU_SPECS
from swm.pricing.calculator import estimate_workload
from swm.providers import (
    get_configured_providers,
    get_provider,
    resolve_instance,
    ALL_PROVIDERS,
    PROVIDER_SLUGS,
)
from swm.providers.base import InstanceStatus

console = Console(log_path=False)


def _preflight_pull(
    storage_slug: str,
    bucket: str,
    workspace: str,
    volume_gb: int,
) -> tuple[list[str], int, int]:
    """Run pre-flight size check and return (extra_excludes, total_bytes, total_files).

    Runs locally — no SSH session needed.
    Returns an empty exclude list when the workspace fits.
    Raises ``SystemExit`` if the user aborts.
    """
    from swm.bootstrap import preflight_check, _humanize

    check = preflight_check(storage_slug, bucket, workspace, volume_gb)

    if check.fits:
        return [], check.workspace_bytes, 0

    if check.dir_sizes:
        table = Table(title="Directory Breakdown", show_lines=True)
        table.add_column("Directory", style="bold")
        table.add_column("Size", justify="right", style="cyan")

        for d, size in sorted(check.dir_sizes.items(), key=lambda x: -x[1]):
            table.add_row(f"{d}/", _humanize(size))

        console.print()
        console.print(table)

    console.print(
        f"\n  Workspace: [bold]{_humanize(check.workspace_bytes)}[/bold]"
        f"  Disk: [bold]{_humanize(check.available_bytes)}[/bold]"
        f"  Over by: [bold red]{_humanize(check.overshoot)}[/bold red]"
    )
    console.print()
    console.print("[bold]Options:[/bold]")
    console.print("  1. Exclude directories (comma-separated names from the table)")
    console.print("  2. Continue anyway (risk running out of disk)")
    console.print("  3. Abort")

    choice = click.prompt(
        "\nExclude dirs, 'continue', or 'abort'",
        default="abort",
    ).strip()

    if choice.lower() == "abort":
        raise SystemExit("Aborted — workspace too large for disk.")

    if choice.lower() == "continue":
        console.print("[yellow]⚠ Proceeding — disk may fill up[/yellow]")
        return [], check.workspace_bytes, 0

    extra = [f"{d.strip()}/**" for d in choice.split(",") if d.strip()]
    if extra:
        remaining = check.workspace_bytes - sum(
            check.dir_sizes.get(d.strip().rstrip("/"), 0)
            for d in choice.split(",")
        )
        console.print(
            f"  Adjusted size: [bold]{_humanize(remaining)}[/bold] "
            f"(excluding {', '.join(d.strip() for d in choice.split(','))})"
        )
    return extra, check.workspace_bytes, 0


_WORKFLOW_EPILOG = """\b
Workflow:
  gpus              Search live GPU availability and pricing
  pod create        Provision a new GPU instance
  ssh <id>          SSH into a running instance
  setup install     Install frameworks (ComfyUI, SwarmUI, ...)
  setup start/stop  Start or stop installed frameworks
  sync pull/push    Sync workspace with cloud storage
  costs live        Show running cost of active pods
  costs summary     Spending breakdown by provider/GPU
  pod down          Push workspace and terminate
"""


@click.group(epilog=_WORKFLOW_EPILOG)
@click.version_option(__version__, prog_name="swm")
def main():
    """swm — Cloud GPU workflow manager for ComfyUI / SwarmUI."""


# ── GPU search ──────────────────────────────────────────────────────

_SLUG_TO_NAME = {cls().slug: cls().name for cls in ALL_PROVIDERS}


def _provider_display(slug_or_name: str) -> str:
    """Map a provider slug to its human-readable name, or pass through."""
    return _SLUG_TO_NAME.get(slug_or_name, slug_or_name)


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
@click.option("--limit", "-n", default=20, type=int, help="Max rows to show (default: 20)")
@click.option("--all", "show_all", is_flag=True, help="Show all results (no pagination)")
def gpus(gpu: str | None, gpu_count: int | None, max_price: float | None,
         provider: str | None, secure: bool, sort_by: str,
         limit: int, show_all: bool):
    """Search live GPU availability and pricing across all providers.

    \b
    Queries configured providers in real-time and supplements with
    static reference pricing for providers you haven't configured yet.

    \b
    Examples:
      swm gpus                        # everything
      swm gpus -g h200                # H200s across all providers
      swm gpus -g h200 -c 4           # 4×H200 configs only
      swm gpus -g h200 --secure       # secure cloud only
      swm gpus --max-price 4          # under $4/hr
      swm gpus -p vastai              # one provider
    """
    from swm.providers.base import GpuInfo

    # --- collect from CloudProvider implementations ---
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
            is_configured = p.is_configured()
            tag = "" if is_configured else " [dim](static)[/dim]"
            spin.update(f"Querying {label}…")
            try:
                results = p.list_gpus(gpu_count=gpu_count)
                all_gpus.extend(results)
                console.log(f"[green]✓[/green] {label}{tag} — {len(results)} GPUs")
            except Exception as exc:
                console.log(f"[red]✗[/red] {label} — {exc}")

        # --- add static rows from OFFERINGS for providers without CloudProvider ---
        impl_names = {cls().name for cls in ALL_PROVIDERS}
        provider_needle = provider.lower() if provider else None

        static_count = 0
        for o in OFFERINGS:
            if o.provider in impl_names:
                continue
            if provider_needle and provider_needle not in o.provider.lower():
                continue
            if gpu_count is not None and o.min_gpus != gpu_count:
                continue
            vram = GPU_SPECS.get(o.gpu)
            all_gpus.append(GpuInfo(
                provider=o.provider,
                type_id=o.instance_type or o.gpu,
                display_name=o.gpu.upper(),
                vram_gb=vram.vram_gb if vram else 0,
                gpu_count=o.min_gpus,
                on_demand_price=o.on_demand,
                spot_price=o.spot,
                stock_level="",
                secure_cloud=bool(o.security),
            ))
            static_count += 1

        if static_count:
            console.log(f"[dim]+ {static_count} static reference offerings[/dim]")

    # --- apply filters ---
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

    if not all_gpus:
        console.print("[yellow]No GPUs found matching filters.[/yellow]")
        return

    # --- sort ---
    if sort_by == "vram":
        all_gpus.sort(key=lambda g: (-g.vram_gb, g.on_demand_price or 999))
    elif sort_by == "provider":
        all_gpus.sort(key=lambda g: (_provider_display(g.provider), g.on_demand_price or 999))
    else:
        all_gpus.sort(key=lambda g: (g.on_demand_price or 999, g.gpu_count))

    # --- paginate ---
    total = len(all_gpus)
    truncated = False
    if not show_all and total > limit:
        all_gpus = all_gpus[:limit]
        truncated = True

    # --- render ---
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
    table.add_column("Secure", justify="center")

    stock_styles = {
        "High": "green", "Medium": "yellow", "Low": "red",
        "None": "red bold", "available": "green", "unavailable": "red",
    }

    for g in all_gpus:
        ss = stock_styles.get(g.stock_level, "dim")
        g_flag = f'"{g.type_id}"' if " " in g.type_id else g.type_id
        table.add_row(
            _provider_display(g.provider),
            g.display_name,
            g_flag,
            f"{g.vram_gb} GB" if g.vram_gb else "—",
            str(g.gpu_count),
            f"${g.on_demand_price:.2f}" if g.on_demand_price else "—",
            f"${g.spot_price:.2f}" if g.spot_price else "—",
            f"[{ss}]{g.stock_level or '—'}[/{ss}]",
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

    console.print(
        "[dim]Copy the [bold]-g[/bold] value into [bold]swm pod create -g <value>[/bold][/dim]"
    )
    console.print()

    gpu_hint = gpu.lower() if gpu else "<gpu>"
    count_hint = f" --gpu-count {gpu_count}" if gpu_count and gpu_count > 1 else ""
    console.print(
        f"[bold]Next →[/bold]  swm pod create -p <provider> -g {gpu_hint} "
        f"-n <name>{count_hint}"
    )
    console.print(
        "[bold]Then →[/bold]  swm setup install <framework> <provider>:<id>"
    )


# ── config ──────────────────────────────────────────────────────────


@main.group(name="config")
def config_group():
    """Manage configuration (API keys, defaults, preferences)."""


@config_group.command(name="set")
@click.argument("key")
@click.argument("value")
def config_set(key: str, value: str):
    """Set a config value.  Example: swm config set runpod.api_key sk-xxx"""
    cfg.set_value(key, value)
    display = value if "key" not in key.lower() else value[:4] + "****"
    console.print(f"[green]✓[/green] {key} = {display}")


@config_group.command(name="get")
@click.argument("key")
def config_get(key: str):
    """Get a config value."""
    val = cfg.get(key)
    if val is None:
        console.print(f"[yellow]⚠[/yellow]  {key} is not set")
    else:
        display = str(val)
        if "key" in key.lower() or "secret" in key.lower():
            display = display[:4] + "****" if len(display) > 4 else "****"
        console.print(f"{key} = {display}")


@config_group.command(name="list")
def config_list():
    """Show all configuration values."""
    data = cfg.load()
    if not data:
        console.print(
            "[dim]No configuration set yet. "
            "Run [bold]swm config set <key> <value>[/bold] to get started.[/dim]"
        )
        return
    _print_nested(data)


@config_group.command(name="path")
def config_path():
    """Show the config file location."""
    console.print(str(cfg.CONFIG_FILE))


@config_group.command(name="delete")
@click.argument("key")
def config_delete(key: str):
    """Remove a config key."""
    if cfg.delete(key):
        console.print(f"[green]✓[/green] Deleted {key}")
    else:
        console.print(f"[yellow]⚠[/yellow]  {key} not found")


def _print_nested(d: dict, prefix: str = "") -> None:
    for k, v in d.items():
        full = f"{prefix}{k}"
        if isinstance(v, dict):
            _print_nested(v, f"{full}.")
        else:
            display = str(v)
            if "key" in full.lower() or "secret" in full.lower():
                display = display[:4] + "****" if len(display) > 4 else "****"
            console.print(f"  {full} = {display}")


# ── pricing ─────────────────────────────────────────────────────────


@main.group()
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


# ── pod management ─────────────────────────────────────────────────


@main.group()
def pod():
    """Manage cloud GPU instances across providers."""


@pod.command(name="list")
@click.option("--provider", "-p", default=None, help="Filter to one provider slug")
def pod_list(provider: str | None):
    """List GPU instances across all configured providers."""
    providers = (
        [get_provider(provider)] if provider else get_configured_providers()
    )
    if not providers:
        console.print(
            "[yellow]No providers configured.[/yellow]\n"
            "Run [bold]swm config set runpod.api_key <key>[/bold] to get started,\n"
            "or configure AWS / GCP / CoreWeave credentials."
        )
        return

    all_instances = []
    with console.status("Fetching instances…", spinner="dots") as spin:
        for p in providers:
            spin.update(f"Querying {p.name}…")
            try:
                insts = p.list_instances()
                all_instances.extend(insts)
                console.log(f"[green]✓[/green] {p.name} — {len(insts)} instances")
            except Exception as e:
                console.log(f"[red]✗[/red] {p.name}: {e}")

    if not all_instances:
        console.print("[dim]No active instances found.[/dim]")
        return

    table = Table(
        title="GPU Instances", title_style="bold", show_lines=True
    )
    table.add_column("ID", style="dim")
    table.add_column("Provider", style="bold")
    table.add_column("Name")
    table.add_column("GPU")
    table.add_column("×", justify="center")
    table.add_column("Status")
    table.add_column("$/hr", justify="right")
    table.add_column("Uptime", justify="right")
    table.add_column("SSH")

    for i in sorted(all_instances, key=lambda x: x.provider):
        table.add_row(
            i.qualified_id,
            i.provider,
            i.name,
            i.gpu_type,
            str(i.gpu_count),
            i.status_rich,
            f"${i.cost_per_hr:.2f}" if i.cost_per_hr else "—",
            i.uptime_display,
            i.ssh_command or "—",
        )

    console.print()
    console.print(table)
    console.print()
    console.print("[dim]Connect:  swm ssh <id>    Run:  swm run <id> <command>[/dim]")


@pod.command()
@click.option("--provider", "-p", required=True, type=click.Choice(list(PROVIDER_SLUGS), case_sensitive=False), help="Cloud provider")
@click.option("--gpu", "-g", default="h200", help="GPU type (h200, b200, etc.)")
@click.option("--name", "-n", required=True, help="Instance name")
@click.option("--workspace", "-w", default=None, help="Existing workspace to restore (creates new if omitted)")
@click.option("--bucket", "-b", default=None, help="Storage bucket (provider:bucket, e.g. b2:my-bucket)")
@click.option("--no-storage", is_flag=True, help="Skip automatic storage setup")
@click.option("--volume", default=100, type=int, help="Persistent volume size in GB")
@click.option("--disk", default=40, type=int, help="Container disk size in GB")
@click.option("--image", default="", help="Docker image (provider default if empty)")
@click.option("--cloud-type", default="SECURE", help="RunPod cloud type: SECURE, COMMUNITY, ALL")
@click.option("--ports", default="22/tcp,8888/http,8188/http", help="Ports to expose")
@click.option("--gpu-count", default=1, type=int, help="Number of GPUs")
@click.option("--region", default=None, help="Datacenter/region ID (e.g. US-CA-2)")
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation")
def create(
    provider: str,
    gpu: str,
    name: str,
    workspace: str | None,
    bucket: str | None,
    no_storage: bool,
    volume: int,
    disk: int,
    image: str,
    cloud_type: str,
    ports: str,
    gpu_count: int,
    region: str | None,
    yes: bool,
):
    """Provision a new GPU instance with automatic workspace sync.

    \b
    Injects your SSH public key so the pod starts sshd on a direct TCP
    port.  After the pod is online, swm connects over SSH to install
    s5cmd, configure storage, and pull the workspace — all streamed
    to your terminal.

    \b
    Examples:
      swm pod create -p runpod -g h200 -n video-gen
      swm pod create -p runpod -g h200 -n video-gen -w workspace2
      swm pod create -p runpod -g h200 -n video-gen --no-storage
    """
    from swm.providers.base import CreateConfig
    from swm.remote.ssh import read_ssh_public_key

    p = get_provider(provider)

    try:
        pub_key = read_ssh_public_key()
    except FileNotFoundError as e:
        raise click.ClickException(str(e))

    ws_name = None
    storage_prov = None
    bucket_name = None
    ws_action = ""

    if not no_storage:
        try:
            from swm.storage import resolve_bucket
            from swm.bootstrap import next_workspace_name

            storage_prov, bucket_name = resolve_bucket(bucket)
            if workspace:
                ws_name = workspace
                ws_action = f"restore [cyan]{workspace}[/cyan]"
            else:
                ws_name = next_workspace_name(storage_prov, bucket_name)
                ws_action = f"new [cyan]{ws_name}[/cyan]"
        except Exception:
            pass

    config = CreateConfig(
        name=name,
        gpu_type=gpu,
        gpu_count=gpu_count,
        volume_gb=volume,
        container_disk_gb=disk,
        image=image,
        region=region,
        cloud_type=cloud_type,
        ports=ports,
        env={"PUBLIC_KEY": pub_key},
    )

    console.print()
    console.print(f"[bold]Creating {p.name} instance:[/bold]")
    console.print(f"  Name:       {name}")
    console.print(f"  GPU:        {gpu} × {gpu_count}")
    console.print(f"  Volume:     {volume} GB")
    console.print(f"  Disk:       {disk} GB")
    console.print(f"  Image:      {image or '(provider default)'}")
    if provider == "runpod":
        console.print(f"  Cloud:      {cloud_type}")
        console.print(f"  Ports:      {ports}")
    if ws_name:
        console.print(f"  Workspace:  {ws_action} on {storage_prov.slug}:{bucket_name}")
    console.print()

    if not yes and not click.confirm("Proceed?"):
        console.print("[dim]Cancelled.[/dim]")
        return

    try:
        with console.status(
            f"Creating {p.name} instance…", spinner="dots"
        ):
            inst = p.create_instance(config)
    except Exception as e:
        raise click.ClickException(str(e))

    console.print()
    console.print(f"[green]✓ Instance created[/green] ({inst.qualified_id})")
    if inst.cost_per_hr:
        console.print(f"  Cost: ${inst.cost_per_hr:.2f}/hr")

    # ── cost tracking (best-effort, record immediately) ──
    try:
        from swm.costs.tracker import record_start
        from swm.costs.budget import check_budget

        record_start(inst, workspace=ws_name)
        warning = check_budget(provider, inst.cost_per_hr)
        if warning:
            for line in warning.splitlines():
                console.print(f"  [yellow]⚠ {line}[/yellow]")
    except Exception:
        pass

    from swm.bootstrap import wait_for_ssh
    try:
        with console.status("Waiting for SSH…", spinner="dots"):
            inst = wait_for_ssh(p, inst.id)
    except TimeoutError as e:
        console.print(f"[yellow]⚠ {e}[/yellow]")

    cfg.set_value(f"pods.{inst.id}.provider", provider)
    cfg.set_value(f"pods.{inst.id}.name", name)

    if ws_name and storage_prov and bucket_name:
        console.print(f"\n[bold]Bootstrapping storage over SSH…[/bold]")
        from swm.remote.ssh import session_from_instance
        from swm.bootstrap import configure_storage, workspace_pull

        try:
            with session_from_instance(inst) as sess:
                with console.status("Installing s5cmd & configuring storage…", spinner="dots"):
                    configure_storage(sess, storage_prov.slug, bucket=bucket_name)
                console.print("[green]✓[/green] Storage configured")
                workspace_pull(sess, storage_prov.slug, bucket_name, ws_name)

            cfg.set_value(f"pods.{inst.id}.workspace", ws_name)
            cfg.set_value(f"pods.{inst.id}.storage", f"{storage_prov.slug}:{bucket_name}")
        except Exception as e:
            console.print(f"[yellow]⚠ Bootstrap failed: {e}[/yellow]")
            console.print(
                "  Retry with: [bold]swm setup storage[/bold] "
                f"{inst.qualified_id} && [bold]swm sync pull[/bold] {inst.qualified_id}"
            )

    console.print(f"\n[bold green]✓ Pod ready![/bold green]")
    console.print(f"  ID:        {inst.qualified_id}")
    if inst.cost_per_hr:
        console.print(f"  Cost:      ${inst.cost_per_hr:.2f}/hr")
    if inst.ssh_command:
        console.print(f"  SSH:       {inst.ssh_command}")
    if ws_name and storage_prov:
        console.print(f"  Workspace: {ws_name} on {storage_prov.slug}:{bucket_name}")
    console.print(
        f"\n  Shut down:  [bold]swm pod down {inst.qualified_id}[/bold]"
    )


@pod.command()
@click.argument("instance_id")
def start(instance_id: str):
    """Start a stopped instance.  Accepts 'provider:id' or bare id."""
    try:
        with console.status("Starting instance…", spinner="dots"):
            provider, raw_id = resolve_instance(instance_id)
            inst = provider.start_instance(raw_id)
    except Exception as e:
        raise click.ClickException(str(e))

    console.print(f"[green]✓[/green] {provider.name} instance {raw_id}: {inst.status_rich}")
    if inst.ssh_command:
        console.print(f"  SSH: {inst.ssh_command}")

    try:
        from swm.costs.tracker import record_start
        from swm.costs.budget import check_budget

        record_start(inst)
        warning = check_budget(provider.slug, inst.cost_per_hr)
        if warning:
            for line in warning.splitlines():
                console.print(f"  [yellow]⚠ {line}[/yellow]")
    except Exception:
        pass


@pod.command()
@click.argument("instance_id")
def stop(instance_id: str):
    """Stop a running instance (preserves volume)."""
    try:
        with console.status("Stopping instance…", spinner="dots"):
            provider, raw_id = resolve_instance(instance_id)
            inst = provider.stop_instance(raw_id)
    except Exception as e:
        raise click.ClickException(str(e))

    console.print(f"[green]✓[/green] {provider.name} instance {raw_id}: {inst.status_rich}")

    try:
        from swm.costs.tracker import record_stop
        record_stop(raw_id, provider.slug)
    except Exception:
        pass


@pod.command()
@click.argument("instance_id")
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation")
def terminate(instance_id: str, yes: bool):
    """Terminate an instance and delete its volume. This is irreversible."""
    try:
        provider, raw_id = resolve_instance(instance_id)
    except Exception as e:
        raise click.ClickException(str(e))

    if not yes:
        console.print(
            f"[bold red]This will permanently destroy {provider.name} "
            f"instance {raw_id} and its volume.[/bold red]"
        )
        if not click.confirm("Are you sure?"):
            console.print("[dim]Cancelled.[/dim]")
            return

    try:
        with console.status("Terminating instance…", spinner="dots"):
            provider.terminate_instance(raw_id)
    except Exception as e:
        raise click.ClickException(str(e))

    console.print(f"[green]✓[/green] {provider.name} instance {raw_id} terminated.")

    try:
        from swm.costs.tracker import record_stop
        record_stop(raw_id, provider.slug)
    except Exception:
        pass


@pod.command()
@click.argument("instance_id")
def status(instance_id: str):
    """Show detailed status of one instance."""
    try:
        with console.status("Fetching status…", spinner="dots"):
            provider, raw_id = resolve_instance(instance_id)
            instances = provider.list_instances()
        inst = next((i for i in instances if i.id == raw_id), None)
    except Exception as e:
        raise click.ClickException(str(e))

    if inst is None:
        raise click.ClickException(f"Instance {raw_id} not found on {provider.name}")

    console.print()
    console.print(f"[bold]{inst.name or inst.id}[/bold]  ({inst.qualified_id})")
    console.print(f"  Provider:   {provider.name}")
    console.print(f"  GPU:        {inst.gpu_type} × {inst.gpu_count}")
    console.print(f"  Status:     {inst.status_rich}")
    if inst.cost_per_hr:
        console.print(f"  Cost:       ${inst.cost_per_hr:.2f}/hr")
    console.print(f"  Uptime:     {inst.uptime_display}")
    if inst.image:
        console.print(f"  Image:      {inst.image}")
    if inst.volume_gb:
        console.print(f"  Volume:     {inst.volume_gb} GB")
    if inst.ip_address:
        console.print(f"  IP:         {inst.ip_address}")
    if inst.ssh_command:
        console.print(f"  SSH:        {inst.ssh_command}")
    if inst.ports:
        port_str = ", ".join(f"{k}→{v}" for k, v in inst.ports.items())
        console.print(f"  Ports:      {port_str}")


@pod.command(name="down")
@click.argument("instance_id")
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation")
@click.option("--no-sync", is_flag=True, help="Skip workspace push before termination")
def pod_down(instance_id: str, yes: bool, no_sync: bool):
    """Push workspace to storage and terminate the instance.

    \b
    Non-destructive: copies /workspace/ to your storage bucket (never
    deletes remote files), then terminates the pod.

    Example: swm pod down runpod:abc123
    """
    try:
        provider, raw_id = resolve_instance(instance_id)
    except Exception as e:
        raise click.ClickException(str(e))

    meta = cfg.get(f"pods.{raw_id}")
    has_workspace = meta and meta.get("workspace") and meta.get("storage")

    if not yes:
        console.print(f"\n[bold]Shutting down {provider.name} instance {raw_id}[/bold]")
        if has_workspace:
            console.print(f"  Workspace: {meta['workspace']}")
            console.print(f"  Storage:   {meta['storage']}")
            console.print(f"  Action:    push workspace → terminate pod")
        else:
            console.print(f"  Action:    terminate pod (no workspace tracked)")
        console.print()
        if not click.confirm("Proceed?"):
            console.print("[dim]Cancelled.[/dim]")
            return

    if not no_sync and has_workspace:
        try:
            with console.status("Checking instance…", spinner="dots"):
                instances = provider.list_instances()
            inst = next((i for i in instances if i.id == raw_id), None)
        except Exception:
            inst = None

        if inst and inst.ssh_host and inst.status == InstanceStatus.RUNNING:
            from swm.bootstrap import workspace_push
            from swm.remote.ssh import session_from_instance

            slug, bucket = meta["storage"].split(":", 1)
            ws = meta["workspace"]

            console.print(f"\n[bold]Pushing workspace to {meta['storage']}...[/bold]")
            try:
                with session_from_instance(inst) as sess:
                    workspace_push(sess, slug, bucket, ws)
                console.print("[green]✓ Workspace synced[/green]")
            except Exception as e:
                console.print(f"[yellow]⚠ Sync failed: {e}[/yellow]")
                if not click.confirm("Terminate anyway? (workspace may be lost)"):
                    console.print("[dim]Cancelled.[/dim]")
                    return
        else:
            console.print("[yellow]⚠ Instance not running — skipping sync[/yellow]")

    try:
        with console.status("Terminating instance…", spinner="dots"):
            provider.terminate_instance(raw_id)
    except Exception as e:
        raise click.ClickException(str(e))

    try:
        from swm.costs.tracker import record_stop
        record_stop(raw_id, provider.slug)
    except Exception:
        pass

    cfg.delete(f"pods.{raw_id}")

    console.print(f"\n[green]✓ {provider.name} instance {raw_id} terminated.[/green]")
    if has_workspace:
        console.print(
            f"  Workspace [cyan]{meta['workspace']}[/cyan] preserved in "
            f"{meta['storage']}."
        )
        console.print(
            f"  Restore later: [bold]swm pod create -p {provider.slug} "
            f"-g <gpu> -n <name> -w {meta['workspace']}[/bold]"
        )


@pod.command(name="gpus", hidden=True, deprecated=True)
@click.option("--provider", "-p", default=None, help="Filter to one provider")
@click.option("--gpu", "-g", default=None, help="Filter by GPU type")
@click.pass_context
def pod_gpus_alias(ctx: click.Context, provider: str | None, gpu: str | None):
    """Alias for 'swm gpus'. Use 'swm gpus' instead."""
    console.print("[dim]Hint: use 'swm gpus' directly for more filters.[/dim]\n")
    ctx.invoke(gpus, gpu=gpu, provider=provider)


# ── remote / ssh ────────────────────────────────────────────────────


def _instance_for(instance_id: str):
    """Resolve an ID and fetch the full Instance object."""
    with console.status("Resolving instance…", spinner="dots"):
        provider, raw_id = resolve_instance(instance_id)
        instances = provider.list_instances()
    inst = next((i for i in instances if i.id == raw_id), None)
    if inst is None:
        raise click.ClickException(f"Instance {raw_id} not found on {provider.name}")
    return inst


def _framework_url(inst, port: int) -> str | None:
    """Build the public URL for a framework running on *inst*."""
    from swm.providers.base import Instance

    provider = (inst.provider or "").lower()
    if provider == "runpod":
        return f"https://{inst.id}-{port}.proxy.runpod.net"
    if provider == "vastai":
        if inst.ip_address:
            mapped = (inst.ports or {}).get(port)
            if mapped:
                return f"http://{inst.ip_address}:{mapped}"
    if inst.ip_address:
        return f"http://{inst.ip_address}:{port}"
    return None


def _probe_url(url: str, timeout: int = 60) -> bool:
    """Try reaching *url* with retries over *timeout* seconds. Returns True if reachable."""
    import httpx
    import time

    deadline = time.monotonic() + timeout
    interval = 3
    while time.monotonic() < deadline:
        try:
            r = httpx.get(url, timeout=5, follow_redirects=True)
            if r.status_code < 500:
                return True
        except (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError, OSError):
            pass
        remaining = deadline - time.monotonic()
        time.sleep(min(interval, max(remaining, 0)))
    return False


@main.command(name="ssh")
@click.argument("instance_id")
def ssh_connect(instance_id: str):
    """Open an interactive SSH session.

    Example: swm ssh runpod:abc123
    """
    from swm.remote.ssh import interactive_ssh

    inst = _instance_for(instance_id)
    console.print(
        f"[bold]Connecting to {inst.name or inst.id}[/bold] "
        f"({inst.provider}) via SSH…"
    )
    try:
        code = interactive_ssh(inst)
    except Exception as e:
        raise click.ClickException(str(e))
    raise SystemExit(code)


@main.command(context_settings={"ignore_unknown_options": True})
@click.argument("instance_id")
@click.argument("command", nargs=-1, type=click.UNPROCESSED, required=True)
@click.option("--quiet", "-q", is_flag=True, help="Suppress real-time output")
def run(instance_id: str, command: tuple[str, ...], quiet: bool):
    """Run a command on a remote instance.

    Examples:\n
      swm run runpod:abc123 nvidia-smi\n
      swm run runpod:abc123 -- ls -la /workspace
    """
    from swm.remote.ssh import session_from_instance

    inst = _instance_for(instance_id)
    cmd_str = " ".join(command)

    if not quiet:
        console.print(
            f"[dim]>>> {inst.provider}:{inst.id}[/dim] $ {cmd_str}\n"
        )

    try:
        with session_from_instance(inst) as sess:
            code, _, _ = sess.exec(cmd_str, stream=not quiet)
    except Exception as e:
        raise click.ClickException(str(e))

    if not quiet:
        style = "green" if code == 0 else "red"
        console.print(f"\n[{style}]Exit code: {code}[/{style}]")
    raise SystemExit(code)


@main.command()
@click.argument("instance_id")
@click.argument("local_path", type=click.Path(exists=True))
@click.argument("remote_path", default="")
@click.option("-r", "--recursive", is_flag=True, help="Upload a directory recursively")
def upload(instance_id: str, local_path: str, remote_path: str, recursive: bool):
    """Upload a file or directory to a running instance.

    \b
    Remote path defaults to /workspace/. If a relative path is given
    (no leading /), it is placed under /workspace/.

    \b
    Examples:
      swm upload runpod:abc123 ./model.safetensors
      swm upload runpod:abc123 ./model.safetensors models/
      swm upload runpod:abc123 ./loras/ models/loras -r
    """
    from pathlib import Path
    from swm.remote.ssh import session_from_instance

    inst = _instance_for(instance_id)

    if not remote_path:
        remote_path = f"/workspace/{Path(local_path).name}"
    elif not remote_path.startswith("/"):
        remote_path = f"/workspace/{remote_path}"
    if remote_path.endswith("/"):
        remote_path = remote_path + Path(local_path).name

    if os.path.isdir(local_path):
        recursive = True

    console.print(
        f"[bold]Uploading[/bold] {local_path} → "
        f"{inst.provider}:{inst.id}:{remote_path}"
    )

    try:
        with session_from_instance(inst) as sess, \
             console.status("Uploading…", spinner="dots"):
            sess.upload(local_path, remote_path, recursive=recursive)
    except Exception as e:
        raise click.ClickException(str(e))

    console.print("[green]✓ Upload complete[/green]")


@main.command()
@click.argument("instance_id")
@click.argument("remote_path")
@click.option("-d", "--dir", "local_dir", default=".", type=click.Path(), help="Local directory to save into (default: current dir)")
def download(instance_id: str, remote_path: str, local_dir: str):
    """Download a file or directory from a running instance.

    \b
    Directories are transferred via tar-over-SSH (compressed stream) which
    is significantly faster than scp -r for multi-file directories.
    If remote_path doesn't start with /, it is treated as relative to /workspace/.

    \b
    Examples:
      swm download runpod:abc123 output.mp4
      swm download runpod:abc123 output.mp4 -d ~/Downloads
      swm download runpod:abc123 ComfyUI/output/ -d ./results
    """
    from pathlib import Path
    from swm.remote.ssh import session_from_instance

    inst = _instance_for(instance_id)

    if not remote_path.startswith("/"):
        remote_path = f"/workspace/{remote_path}"

    remote_path = remote_path.rstrip("/")

    try:
        with session_from_instance(inst) as sess:
            with console.status("Checking remote path…", spinner="dots"):
                is_dir = sess.is_directory(remote_path)

            local_dir = str(Path(local_dir).expanduser())

            if is_dir:
                import tempfile
                base_name = Path(remote_path).name
                final_dest = Path(local_dir) / base_name
                if final_dest.exists():
                    n = 1
                    while (Path(local_dir) / f"{base_name}_{n}").exists():
                        n += 1
                    final_dest = Path(local_dir) / f"{base_name}_{n}"
                    console.print(
                        f"  [yellow]⚠ Destination already exists — saving to "
                        f"[bold]{final_dest.name}[/bold] instead[/yellow]"
                    )

                console.print(
                    f"[bold]Downloading directory[/bold] "
                    f"{inst.provider}:{inst.id}:{remote_path} → {final_dest}"
                )
                console.print("  [dim]Using tar stream (compressed)[/dim]")

                with console.status("Counting files…", spinner="dots"):
                    total = sess.file_count(remote_path)

                from rich.progress import (
                    Progress, SpinnerColumn, BarColumn,
                    TaskProgressColumn, TimeRemainingColumn, TextColumn,
                )

                # Ensure local_dir exists before creating a temp dir inside it.
                Path(local_dir).mkdir(parents=True, exist_ok=True)

                # Extract into a sibling temp dir, then rename atomically so
                # the original directory is never modified on collision.
                # Use manual cleanup (not context manager) so a rename failure
                # doesn't delete the temp dir and lose the downloaded data.
                tmpdir_obj = tempfile.TemporaryDirectory(dir=local_dir)
                tmpdir = tmpdir_obj.name
                try:
                    with Progress(
                        SpinnerColumn(),
                        TextColumn("[bold]{task.description}"),
                        BarColumn(),
                        TaskProgressColumn(),
                        TextColumn("[dim]{task.completed}/{task.total} files"),
                        TimeRemainingColumn(),
                        console=console,
                        transient=True,
                    ) as progress:
                        task = progress.add_task("Streaming…", total=total or None)

                        def _on_member(name: str) -> None:
                            if not name.endswith("/"):  # skip directory entries
                                progress.advance(task)
                                progress.update(task, description=Path(name).name[:40])

                        sess.download_dir(remote_path, tmpdir, progress_callback=_on_member)

                    extracted = Path(tmpdir) / base_name
                    extracted.rename(final_dest)
                finally:
                    tmpdir_obj.cleanup()
            else:
                Path(local_dir).mkdir(parents=True, exist_ok=True)
                dest = str(Path(local_dir) / Path(remote_path).name)
                console.print(
                    f"[bold]Downloading[/bold] "
                    f"{inst.provider}:{inst.id}:{remote_path} → {dest}"
                )
                with console.status("Downloading…", spinner="dots"):
                    sess.download(remote_path, dest)

    except Exception as e:
        raise click.ClickException(str(e))

    console.print("[green]✓ Download complete[/green]")


# ── setup ───────────────────────────────────────────────────────────


@main.group()
def setup():
    """Install, start, and stop frameworks on a running instance."""


@setup.command(name="storage")
@click.argument("instance_id")
@click.option(
    "--provider", "-p",
    type=click.Choice(["b2", "gcs", "s3", "all"], case_sensitive=False),
    default="all",
    help="Which storage backend to configure (default: all configured)",
)
def setup_storage(instance_id: str, provider: str):
    """Configure cloud storage (s5cmd) on a running instance.

    \b
    Reads S3-compatible credentials from swm config and installs s5cmd
    on the pod.  Credentials are passed as env vars per command — no
    config files written to the pod.

    Examples:
      swm setup storage runpod:abc123            # configure all
      swm setup storage runpod:abc123 -p b2      # B2 only
      swm setup storage runpod:abc123 -p gcs     # GCS only
      swm setup storage runpod:abc123 -p s3      # S3 only
    """
    from swm import config as _cfg
    from swm.bootstrap import configure_storage
    from swm.remote.ssh import session_from_instance

    inst = _instance_for(instance_id)
    console.print(
        f"\n[bold]Configuring storage on {inst.name or inst.id}[/bold] "
        f"({inst.provider})"
    )

    slugs_to_configure: list[str] = []

    if provider in ("b2", "all"):
        if _cfg.get("b2.key_id") and _cfg.get("b2.app_key"):
            slugs_to_configure.append("b2")
        elif provider == "b2":
            raise click.ClickException(
                "B2 credentials not set. Run:\n"
                "  swm config set b2.key_id <id>\n"
                "  swm config set b2.app_key <key>"
            )

    if provider in ("gcs", "all"):
        if _cfg.get("gcs.hmac_access") and _cfg.get("gcs.hmac_secret"):
            slugs_to_configure.append("gcs")
        elif provider == "gcs":
            raise click.ClickException(
                "GCS HMAC credentials not set. Run:\n"
                "  swm config set gcs.hmac_access <key>\n"
                "  swm config set gcs.hmac_secret <secret>"
            )

    if provider in ("s3", "all"):
        if _cfg.get("s3.access_key") and _cfg.get("s3.secret_key"):
            slugs_to_configure.append("s3")
        elif provider == "s3":
            raise click.ClickException(
                "S3 credentials not set. Run:\n"
                "  swm config set s3.access_key <key>\n"
                "  swm config set s3.secret_key <secret>"
            )

    if not slugs_to_configure:
        console.print("[yellow]No storage providers configured in swm config.[/yellow]")
        return

    try:
        sess_ctx = session_from_instance(inst)
    except Exception as e:
        raise click.ClickException(f"Cannot connect to instance: {e}")

    configured = []
    with sess_ctx as sess, console.status("Configuring storage…", spinner="dots") as spin:
        for slug in slugs_to_configure:
            spin.update(f"Configuring {slug}…")
            configure_storage(sess, slug)
            bucket_key = {"b2": "b2.bucket", "gcs": "gcp.bucket", "s3": "s3.bucket"}[slug]
            bucket = _cfg.get(bucket_key)
            configured.append(
                f"{slug} → bucket [cyan]{bucket or '(set with swm config set ' + bucket_key + ' <name>)'}[/cyan]"
            )
            console.log(f"[green]✓[/green] {slug} configured")

    console.print("\n[green]✓ Storage configured on pod[/green]")
    for c in configured:
        console.print(f"  {c}")
    console.print(
        "\n[dim]Sync models with: swm sync pull "
        f"{instance_id} [path][/dim]"
    )


@setup.command(name="install")
@click.argument("framework_name")
@click.argument("instance_id")
def setup_install(framework_name: str, instance_id: str):
    """Install a framework on a running instance.

    \b
    Examples:
      swm setup install comfyui runpod:abc123
      swm setup install axolotl runpod:abc123
      swm setup install llm-studio runpod:abc123

    \b
    See available frameworks: swm setup list
    """
    from swm.bootstrap import install_framework
    from swm.frameworks import get_framework
    from swm.remote.ssh import session_from_instance

    fw = get_framework(framework_name)
    inst = _instance_for(instance_id)
    console.print(
        f"\n[bold]Installing {fw.label} on {inst.name or inst.id}[/bold] "
        f"({inst.provider})"
    )

    with session_from_instance(inst) as sess:
        install_framework(sess, framework_name, console=console)

    console.print(f"\n[green]✓ {fw.label} installed[/green]")
    if fw.ports:
        port = next(iter(fw.ports))
        console.print(f"  Start:  swm setup start {fw.name} {instance_id}")
        console.print(f"  Access: https://<pod-id>-{port}.proxy.runpod.net")
    else:
        console.print(f"  Run:    swm run {instance_id} 'cd {fw.install_dir} && {fw.launch_cmd}'")


@setup.command(name="start")
@click.argument("framework_name")
@click.argument("instance_id")
@click.option("--port", "-p", type=int, default=None, help="Override the default listen port")
def setup_start(framework_name: str, instance_id: str, port: int | None):
    """Start a framework on a running instance.

    \b
    Examples:
      swm setup start comfyui runpod:abc123
      swm setup start swarmui runpod:abc123 --port 8888
    """
    from swm.bootstrap import start_framework
    from swm.frameworks import get_framework
    from swm.remote.ssh import session_from_instance

    fw = get_framework(framework_name)
    inst = _instance_for(instance_id)

    with session_from_instance(inst) as sess:
        start_framework(sess, framework_name, port=port, console=console)

    listen_port = port or (next(iter(fw.ports)) if fw.ports else None)
    if listen_port:
        url = _framework_url(inst, listen_port)
        if url:
            with console.status("Waiting for HTTP to become reachable…", spinner="dots"):
                reachable = _probe_url(url, timeout=60)
            if reachable:
                console.print(f"  [green]✓[/green] URL: {url}")
            else:
                console.print(f"  [yellow]⚠ URL not yet reachable after 60 s — framework may still be loading[/yellow]")
                console.print(f"  URL: {url}")
                console.print(f"  Logs: swm run {instance_id} 'tail -f /tmp/{framework_name}.log'")


@setup.command(name="stop")
@click.argument("framework_name")
@click.argument("instance_id")
def setup_stop(framework_name: str, instance_id: str):
    """Stop a framework on a running instance.

    \b
    Examples:
      swm setup stop comfyui runpod:abc123
    """
    from swm.bootstrap import stop_framework
    from swm.remote.ssh import session_from_instance

    inst = _instance_for(instance_id)

    with session_from_instance(inst) as sess:
        stop_framework(sess, framework_name, console=console)


@setup.command(name="list")
def setup_list():
    """List available frameworks.

    \b
    Example: swm setup list
    """
    from rich.table import Table
    from swm.frameworks import list_frameworks

    table = Table(title="Available Frameworks")
    table.add_column("Name", style="bold")
    table.add_column("Label")
    table.add_column("Category")
    table.add_column("Ports")
    table.add_column("Repo")

    for fw in list_frameworks():
        ports = ", ".join(f"{p}/{t}" for p, t in fw.ports.items()) if fw.ports else "—"
        table.add_row(fw.name, fw.label, fw.category, ports, fw.repo)

    console.print(table)


# backward-compat aliases
@setup.command(hidden=True)
@click.argument("instance_id")
@click.option("--link-models", is_flag=True, default=True, help="Symlink /workspace/models into ComfyUI")
def comfyui(instance_id: str, link_models: bool):
    """Install ComfyUI (alias for 'swm setup install comfyui')."""
    from swm.bootstrap import install_framework, link_models_to_comfyui
    from swm.remote.ssh import session_from_instance

    inst = _instance_for(instance_id)
    with session_from_instance(inst) as sess:
        install_framework(sess, "comfyui")
        if link_models:
            link_models_to_comfyui(sess)
    console.print("\n[green]✓ ComfyUI installed[/green]")


@setup.command(hidden=True)
@click.argument("instance_id")
def swarmui(instance_id: str):
    """Install SwarmUI (alias for 'swm setup install swarmui')."""
    from swm.bootstrap import install_framework
    from swm.remote.ssh import session_from_instance

    inst = _instance_for(instance_id)
    with session_from_instance(inst) as sess:
        install_framework(sess, "swarmui")
    console.print("\n[green]✓ SwarmUI installed[/green]")


# ── sync ────────────────────────────────────────────────────────────


@main.group()
def sync():
    """Sync files between cloud storage and running instances."""


@sync.command()
@click.argument("instance_id")
@click.argument("path", default="")
@click.option("--bucket", "-b", default=None, help="Override bucket (provider:bucket)")
@click.option("--dest", "-d", default="/workspace", help="Destination on pod")
@click.option("--force", "-f", is_flag=True, help="Kill any running transfer and start fresh")
def pull(instance_id: str, path: str, bucket: str | None, dest: str, force: bool):
    """Pull workspace from cloud storage to a running instance.

    \b
    Defaults to the pod's tracked workspace. Always non-destructive
    (s5cmd cp --no-clobber — skips existing files, never deletes).

    \b
    Examples:
      swm sync pull runpod:abc123                          # full workspace
      swm sync pull runpod:abc123 ComfyUI/models/          # subfolder only
      swm sync pull runpod:abc123 -b b2:backup-data-13943  # explicit bucket
      swm sync pull runpod:abc123 --force                  # kill stale & restart
    """
    from swm.bootstrap import workspace_pull
    from swm.remote.ssh import session_from_instance

    _, raw_id = resolve_instance(instance_id)
    meta = cfg.get(f"pods.{raw_id}")

    if bucket:
        from swm.storage import resolve_bucket
        sp, bname = resolve_bucket(bucket)
        remote, bucket_name = sp.slug, bname
        ws = path or ""
    elif meta and meta.get("workspace") and meta.get("storage"):
        slug, bucket_name = meta["storage"].split(":", 1)
        remote = slug
        ws = f"{meta['workspace']}/{path}" if path else meta["workspace"]
    else:
        raise click.ClickException(
            "No workspace tracked for this pod. Use -b to specify a bucket."
        )

    inst = _instance_for(instance_id)

    console.print(
        f"\n[bold]Pulling[/bold] {remote}:{bucket_name}/{ws} → "
        f"{inst.name or inst.id}:{dest}"
    )

    with console.status("Running preflight checks…", spinner="dots"):
        extra_excludes, total_bytes, total_files = _preflight_pull(
            remote, bucket_name, ws, volume_gb=inst.volume_gb or 100,
        )

    with session_from_instance(inst) as sess:
        workspace_pull(
            sess, remote, bucket_name, ws,
            dest=dest, extra_excludes=extra_excludes,
            total_bytes=total_bytes, total_files=total_files,
            force=force,
        )

    console.print("\n[green]✓ Pull complete[/green]")


@sync.command()
@click.argument("instance_id")
@click.argument("path", default="/workspace")
@click.option("--bucket", "-b", default=None, help="Override bucket (provider:bucket)")
@click.option("--dest", "-d", default="", help="Override destination path inside bucket")
@click.option("--force", "-f", is_flag=True, help="Kill any running transfer and start fresh")
def push(instance_id: str, path: str, bucket: str | None, dest: str, force: bool):
    """Push workspace from a running instance to cloud storage.

    \b
    Defaults to the pod's tracked workspace. Always non-destructive
    (s5cmd sync --size-only — uploads new/changed files, never deletes).

    \b
    Examples:
      swm sync push runpod:abc123                       # full /workspace
      swm sync push runpod:abc123 /workspace/output     # subfolder
      swm sync push runpod:abc123 --force               # kill stale & restart
    """
    from swm.bootstrap import workspace_push
    from swm.remote.ssh import session_from_instance

    _, raw_id = resolve_instance(instance_id)
    meta = cfg.get(f"pods.{raw_id}")

    if bucket:
        from swm.storage import resolve_bucket
        sp, bname = resolve_bucket(bucket)
        remote, bucket_name = sp.slug, bname
        ws = dest
    elif meta and meta.get("workspace") and meta.get("storage"):
        slug, bucket_name = meta["storage"].split(":", 1)
        remote = slug
        ws = meta["workspace"]
    else:
        raise click.ClickException(
            "No workspace tracked for this pod. Use -b to specify a bucket."
        )

    inst = _instance_for(instance_id)

    console.print(
        f"\n[bold]Pushing[/bold] {inst.name or inst.id}:{path} → "
        f"{remote}:{bucket_name}/{ws}"
    )

    with session_from_instance(inst) as sess:
        workspace_push(sess, remote, bucket_name, ws, src=path, force=force)

    console.print("\n[green]✓ Push complete[/green]")


@sync.command(name="status")
@click.argument("instance_id")
def sync_status(instance_id: str):
    """Show storage sync status on an instance.

    Example: swm sync status runpod:abc123
    """
    from swm.remote.ssh import session_from_instance

    _, raw_id = resolve_instance(instance_id)
    meta = cfg.get(f"pods.{raw_id}")
    inst = _instance_for(instance_id)

    console.print(f"\n[bold]Storage status for {inst.name or inst.id}[/bold]")

    with session_from_instance(inst) as sess, \
         console.status("Checking storage tools…", spinner="dots"):
        code, stdout, _ = sess.exec(
            "command -v s5cmd >/dev/null 2>&1 "
            "&& echo 's5cmd:' && s5cmd version "
            "|| echo '(s5cmd not installed)'",
            stream=False,
        )
    if stdout.strip():
        console.print(f"  {stdout.strip()}")

    if meta and meta.get("workspace") and meta.get("storage"):
        console.print(f"  Workspace: [cyan]{meta['workspace']}[/cyan]")
        console.print(f"  Storage:   [cyan]{meta['storage']}[/cyan]")
    else:
        console.print("  [dim]No workspace tracked for this pod[/dim]")


# ── models (stub) ──────────────────────────────────────────────────


@main.group()
def models():
    """Download, organise, and sync AI models."""


@models.command()
@click.argument("name")
def pull(name: str):
    """Download a model.  Example: swm models pull wan2.2"""
    console.print("[dim]Coming soon — Phase 6[/dim]")


@models.command()
def sync():
    """Sync models to/from cloud storage."""
    console.print("[dim]Coming soon — Phase 6[/dim]")


@models.command(name="list")
def models_list():
    """List available model presets."""
    console.print("[dim]Coming soon — Phase 6[/dim]")


def _session_cost(row, now) -> float | None:
    """Compute cost for a session row (closed or running)."""
    from datetime import datetime
    if row["stopped_at"] and row["estimated_cost"] is not None:
        return row["estimated_cost"]
    if row["cost_per_hr"] is not None:
        started = datetime.fromisoformat(row["started_at"])
        end = datetime.fromisoformat(row["stopped_at"]) if row["stopped_at"] else now
        return round(row["cost_per_hr"] * (end - started).total_seconds() / 3600, 4)
    return None


# ── costs ──────────────────────────────────────────────────────────


@main.group()
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
    from datetime import datetime, timedelta, timezone
    from rich.table import Table

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

    # Aggregate by provider, then by gpu_type.
    from collections import defaultdict

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
    from datetime import datetime, timezone
    from rich.table import Table

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
    from rich.table import Table

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
    from rich.table import Table
    from rich.progress_bar import ProgressBar

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


@costs.command()
@click.option("--provider", "-p", default=None,
              type=click.Choice(["runpod", "vastai"], case_sensitive=False),
              help="Reconcile one provider only")
def reconcile(provider: str | None):
    """Compare local cost records with provider billing APIs.

    Queries RunPod dailyCharges and/or Vast.ai invoices and reports
    any discrepancy with locally tracked spend.
    """
    from rich.table import Table

    from swm.costs.reconcile import reconcile_runpod, reconcile_vastai

    results: list[dict] = []
    targets = []
    if provider is None or provider == "runpod":
        targets.append(("RunPod", reconcile_runpod))
    if provider is None or provider == "vastai":
        targets.append(("Vast.ai", reconcile_vastai))

    for name, fn in targets:
        with console.status(f"Querying {name} billing API…", spinner="dots"):
            try:
                results.append(fn())
            except Exception as e:
                results.append({"provider": name.lower(), "error": str(e)})

    for r in results:
        if "error" in r:
            console.print(f"\n  [yellow]⚠ {r.get('provider', '?')}: {r['error']}[/yellow]")
            continue

        console.print(f"\n[bold]{r['provider']}[/bold] — {r['period']}")
        if r.get("balance") is not None:
            console.print(f"  Account balance: ${r['balance']:.2f}")
        if r.get("current_rate") is not None:
            console.print(f"  Current rate:    ${r['current_rate']:.4f}/hr")

        table = Table()
        table.add_column("Source", min_width=12)
        table.add_column("Total", justify="right")
        table.add_row("Provider API", f"${r['provider_total']:.2f}")
        table.add_row("Local (swm)", f"${r['local_total']:.2f}")

        diff = r["difference"]
        diff_color = "green" if abs(diff) < 1 else "yellow"
        table.add_row(
            "Difference",
            f"[{diff_color}]${diff:+.2f}[/{diff_color}]",
        )

        console.print(table)

        if r.get("details"):
            console.print(f"  [dim]{len(r['details'])} charge records from provider[/dim]")


# ── storage ─────────────────────────────────────────────────────────


@main.group()
def storage():
    """Manage cloud storage buckets (GCS, Backblaze B2, S3)."""


@storage.command(name="list")
@click.option("--provider", "-p", default=None, type=click.Choice(["gcs", "b2", "s3"], case_sensitive=False), help="Filter to one provider")
def storage_list(provider: str | None):
    """List buckets across configured storage providers."""
    from swm.storage import get_configured_storage, get_storage

    sources = [get_storage(provider)] if provider else get_configured_storage()
    if not sources:
        console.print(
            "[yellow]No storage providers configured.[/yellow]\n"
            "  GCS:  swm config set gcp.project <id>\n"
            "  B2:   swm config set b2.key_id <id> && swm config set b2.app_key <key>\n"
            "  S3:   Configure AWS credentials (aws configure)"
        )
        return

    all_buckets = []
    with console.status("Fetching buckets…", spinner="dots") as spin:
        for s in sources:
            spin.update(f"Querying {s.name}…")
            try:
                buckets = s.list_buckets()
                all_buckets.extend(buckets)
                console.log(f"[green]✓[/green] {s.name} — {len(buckets)} buckets")
            except Exception as e:
                console.log(f"[red]✗[/red] {s.name}: {e}")

    if not all_buckets:
        console.print("[dim]No buckets found. Create one with: swm storage create <name> -p <provider>[/dim]")
        return

    table = Table(title="Storage Buckets", title_style="bold", show_lines=True)
    table.add_column("Provider", style="bold")
    table.add_column("Bucket", style="cyan")
    table.add_column("Location")
    table.add_column("Class")
    table.add_column("Created")
    table.add_column("Default", justify="center")

    from swm.storage import resolve_bucket
    try:
        _, default_name = resolve_bucket()
    except Exception:
        default_name = None

    for b in all_buckets:
        table.add_row(
            b.provider,
            b.name,
            b.location or "—",
            b.storage_class or "—",
            b.created or "—",
            "[green]✓[/green]" if b.name == default_name else "",
        )

    console.print()
    console.print(table)


@storage.command()
@click.argument("name")
@click.option("--provider", "-p", required=True, type=click.Choice(["gcs", "b2", "s3"], case_sensitive=False), help="Storage provider")
@click.option("--location", "-l", default="", help="Bucket location/region")
@click.option("--storage-class", "-c", default="", help="Storage class (STANDARD, NEARLINE, allPrivate, etc.)")
def create(name: str, provider: str, location: str, storage_class: str):
    """Create a storage bucket.

    \b
    Examples:
      swm storage create swm-models -p gcs
      swm storage create my-models -p b2
    """
    from swm.storage import get_storage

    s = get_storage(provider)
    try:
        with console.status(f"Creating bucket on {s.name}…", spinner="dots"):
            bucket = s.create_bucket(name, location=location, storage_class=storage_class)
    except Exception as e:
        raise click.ClickException(str(e))

    console.print(f"[green]✓[/green] Bucket created on {s.name}: {bucket.name}")
    if bucket.location:
        console.print(f"  Location:      {bucket.location}")
    if bucket.storage_class:
        console.print(f"  Storage class: {bucket.storage_class}")
    console.print(f"[green]✓[/green] Saved as default {provider} bucket in swm config")


@storage.command(name="ls")
@click.argument("path", default="")
@click.option("--bucket", "-b", default=None, help="Bucket name (default from config). Use 'provider:bucket' for explicit.")
def storage_ls(path: str, bucket: str | None):
    """List contents of a bucket.

    \b
    Examples:
      swm storage ls                      # default bucket root
      swm storage ls models/              # subdirectory
      swm storage ls -b gcs:swm-models    # explicit provider:bucket
      swm storage ls -b b2:my-backup      # backblaze bucket
    """
    from swm.storage import resolve_bucket

    try:
        provider, bucket_name = resolve_bucket(bucket)
    except Exception as e:
        raise click.ClickException(str(e))

    try:
        with console.status("Listing objects…", spinner="dots"):
            objects = provider.ls(bucket_name, prefix=path)
    except Exception as e:
        raise click.ClickException(str(e))

    label = f"{provider.slug}:{bucket_name}/{path}" if path else f"{provider.slug}:{bucket_name}/"
    console.print(f"\n[bold]{label}[/bold]")

    if not objects:
        console.print("[dim]  (empty)[/dim]")
        return

    table = Table(show_header=True, padding=(0, 2))
    table.add_column("Name")
    table.add_column("Size", justify="right")
    table.add_column("Modified")

    for obj in objects:
        style = "bold" if obj.is_directory else ""
        table.add_row(
            f"[{style}]{obj.key}[/{style}]" if style else obj.key,
            obj.size_display,
            obj.modified or "—",
        )

    console.print(table)


@storage.command()
@click.argument("local_path", type=click.Path(exists=True))
@click.argument("remote_path")
@click.option("--bucket", "-b", default=None, help="Target bucket (provider:bucket)")
def upload(local_path: str, remote_path: str, bucket: str | None):
    """Upload a file to a bucket.

    Example: swm storage upload ./model.safetensors models/model.safetensors
    """
    from swm.storage import resolve_bucket

    try:
        provider, bucket_name = resolve_bucket(bucket)
        with console.status("Uploading…", spinner="dots"):
            provider.upload(local_path, bucket_name, remote_path)
    except Exception as e:
        raise click.ClickException(str(e))

    console.print(f"[green]✓[/green] Uploaded to {provider.slug}:{bucket_name}/{remote_path}")


@storage.command()
@click.argument("remote_path")
@click.argument("local_path", type=click.Path())
@click.option("--bucket", "-b", default=None, help="Source bucket (provider:bucket)")
def download(remote_path: str, local_path: str, bucket: str | None):
    """Download a file from a bucket.

    Example: swm storage download models/model.safetensors ./model.safetensors
    """
    from swm.storage import resolve_bucket

    try:
        provider, bucket_name = resolve_bucket(bucket)
        with console.status("Downloading…", spinner="dots"):
            provider.download(bucket_name, remote_path, local_path)
    except Exception as e:
        raise click.ClickException(str(e))

    console.print(f"[green]✓[/green] Downloaded {provider.slug}:{bucket_name}/{remote_path} → {local_path}")


# ── security (stub) ────────────────────────────────────────────────


@main.group()
def security():
    """Verify provider security posture."""


@security.command()
def audit():
    """Run a security audit on current provider configuration."""
    console.print("[dim]Coming soon — Phase 9[/dim]")
