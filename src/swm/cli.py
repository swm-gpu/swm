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

console = Console()


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


@click.group()
@click.version_option(__version__, prog_name="swm")
def main():
    """swm — Cloud GPU workflow manager for ComfyUI / SwarmUI."""


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
    for p in providers:
        try:
            all_instances.extend(p.list_instances())
        except Exception as e:
            console.print(f"[red]✗[/red] {p.name}: {e}")

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
        inst = p.create_instance(config)
    except Exception as e:
        raise click.ClickException(str(e))

    console.print()
    console.print(f"[green]✓ Instance created[/green] ({inst.qualified_id})")
    if inst.cost_per_hr:
        console.print(f"  Cost: ${inst.cost_per_hr:.2f}/hr")

    console.print(f"\n[bold]Waiting for SSH...[/bold]")
    from swm.bootstrap import wait_for_ssh
    try:
        inst = wait_for_ssh(p, inst.id)
    except TimeoutError as e:
        console.print(f"[yellow]⚠ {e}[/yellow]")

    cfg.set_value(f"pods.{inst.id}.provider", provider)
    cfg.set_value(f"pods.{inst.id}.name", name)

    if ws_name and storage_prov and bucket_name:
        console.print(f"\n[bold]Bootstrapping storage over SSH...[/bold]")
        from swm.remote.ssh import session_from_instance
        from swm.bootstrap import configure_storage, workspace_pull

        try:
            with session_from_instance(inst) as sess:
                configure_storage(sess, storage_prov.slug, bucket=bucket_name)
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
        provider, raw_id = resolve_instance(instance_id)
        inst = provider.start_instance(raw_id)
    except Exception as e:
        raise click.ClickException(str(e))

    console.print(f"[green]✓[/green] {provider.name} instance {raw_id}: {inst.status_rich}")
    if inst.ssh_command:
        console.print(f"  SSH: {inst.ssh_command}")


@pod.command()
@click.argument("instance_id")
def stop(instance_id: str):
    """Stop a running instance (preserves volume)."""
    try:
        provider, raw_id = resolve_instance(instance_id)
        inst = provider.stop_instance(raw_id)
    except Exception as e:
        raise click.ClickException(str(e))

    console.print(f"[green]✓[/green] {provider.name} instance {raw_id}: {inst.status_rich}")


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
        provider.terminate_instance(raw_id)
    except Exception as e:
        raise click.ClickException(str(e))

    console.print(f"[green]✓[/green] {provider.name} instance {raw_id} terminated.")


@pod.command()
@click.argument("instance_id")
def status(instance_id: str):
    """Show detailed status of one instance."""
    try:
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
        provider.terminate_instance(raw_id)
    except Exception as e:
        raise click.ClickException(str(e))

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


@pod.command()
@click.option("--provider", "-p", default=None, help="Filter to one provider")
@click.option("--gpu", type=click.Choice(["h200", "b200"], case_sensitive=False), help="Filter by GPU type")
def gpus(provider: str | None, gpu: str | None):
    """Show available GPUs across providers (live where possible)."""
    if provider:
        sources = [get_provider(provider)]
    else:
        configured = get_configured_providers()
        unconfigured_slugs = {
            p().slug for p in ALL_PROVIDERS
        } - {p.slug for p in configured}

        sources = list(configured)
        for slug in unconfigured_slugs:
            sources.append(get_provider(slug))

    all_gpus = []
    live_providers: set[str] = set()
    for p in sources:
        try:
            gpus_list = p.list_gpus()
            all_gpus.extend(gpus_list)
            if p.is_configured():
                live_providers.add(p.slug)
        except Exception:
            pass

    if gpu:
        needle = gpu.lower()
        all_gpus = [
            g for g in all_gpus if needle in g.display_name.lower() or needle in g.type_id.lower()
        ]

    if not all_gpus:
        console.print("[yellow]No GPUs found matching filters.[/yellow]")
        return

    table = Table(
        title="Available GPUs Across Providers",
        title_style="bold",
        show_lines=True,
    )
    table.add_column("Provider", style="bold")
    table.add_column("GPU")
    table.add_column("VRAM", justify="right")
    table.add_column("On-Demand", justify="right")
    table.add_column("Spot", justify="right")
    table.add_column("Min GPUs", justify="center")
    table.add_column("Stock")
    table.add_column("Secure", justify="center")
    table.add_column("Source", style="dim")

    for g in sorted(all_gpus, key=lambda x: (x.on_demand_price or 999)):
        stock_style = {
            "High": "green",
            "Medium": "yellow",
            "Low": "red",
            "None": "red bold",
        }.get(g.stock_level, "dim")

        table.add_row(
            g.provider,
            g.display_name,
            f"{g.vram_gb} GB",
            f"${g.on_demand_price:.2f}" if g.on_demand_price else "—",
            f"${g.spot_price:.2f}" if g.spot_price else "—",
            str(g.min_gpu_count),
            f"[{stock_style}]{g.stock_level or '—'}[/{stock_style}]",
            "[green]✓[/green]" if g.secure_cloud else "[dim]✗[/dim]",
            "live" if g.provider in live_providers else "static",
        )

    console.print()
    console.print(table)
    console.print()
    console.print(
        "[dim]'live' = real-time data from provider API. "
        "'static' = pricing database (configure API key for live data).[/dim]"
    )


# ── remote / ssh ────────────────────────────────────────────────────


def _instance_for(instance_id: str):
    """Resolve an ID and fetch the full Instance object."""
    provider, raw_id = resolve_instance(instance_id)
    instances = provider.list_instances()
    inst = next((i for i in instances if i.id == raw_id), None)
    if inst is None:
        raise click.ClickException(f"Instance {raw_id} not found on {provider.name}")
    return inst


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
        f"({inst.provider}) via SSH..."
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
        with session_from_instance(inst) as sess:
            sess.upload(local_path, remote_path, recursive=recursive)
    except Exception as e:
        raise click.ClickException(str(e))

    console.print("[green]✓ Upload complete[/green]")


@main.command()
@click.argument("instance_id")
@click.argument("remote_path")
@click.option("-d", "--dir", "local_dir", default=".", type=click.Path(), help="Local directory to save into (default: current dir)")
@click.option("-r", "--recursive", is_flag=True, help="Download a directory recursively")
def download(instance_id: str, remote_path: str, local_dir: str, recursive: bool):
    """Download a file or directory from a running instance.

    \b
    If remote_path doesn't start with /, it is treated as relative to
    /workspace/. Downloaded files land in the directory given by --dir.

    \b
    Examples:
      swm download runpod:abc123 output.mp4
      swm download runpod:abc123 output.mp4 -d ~/Downloads
      swm download runpod:abc123 ComfyUI/output/ -r -d ./results
    """
    from pathlib import Path
    from swm.remote.ssh import session_from_instance

    inst = _instance_for(instance_id)

    if not remote_path.startswith("/"):
        remote_path = f"/workspace/{remote_path}"

    dest = local_dir
    if os.path.isdir(dest):
        dest = str(Path(dest) / Path(remote_path).name)

    console.print(
        f"[bold]Downloading[/bold] {inst.provider}:{inst.id}:{remote_path} → "
        f"{dest}"
    )

    try:
        with session_from_instance(inst) as sess:
            sess.download(remote_path, dest, recursive=recursive)
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
    with sess_ctx as sess:
        for slug in slugs_to_configure:
            configure_storage(sess, slug)
            bucket_key = {"b2": "b2.bucket", "gcs": "gcp.bucket", "s3": "s3.bucket"}[slug]
            bucket = _cfg.get(bucket_key)
            configured.append(
                f"{slug} → bucket [cyan]{bucket or '(set with swm config set ' + bucket_key + ' <name>)'}[/cyan]"
            )

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
        install_framework(sess, framework_name)

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
        start_framework(sess, framework_name, port=port)

    listen_port = port or (next(iter(fw.ports)) if fw.ports else None)
    if listen_port:
        console.print(f"  URL: https://{inst.id}-{listen_port}.proxy.runpod.net")


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
        stop_framework(sess, framework_name)


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

    with session_from_instance(inst) as sess:
        code, stdout, _ = sess.exec(
            "command -v s5cmd >/dev/null 2>&1 "
            "&& echo 's5cmd:' && s5cmd version "
            "|| echo '(s5cmd not installed)'",
            stream=True,
        )

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


# ── costs (stub) ───────────────────────────────────────────────────


@main.group()
def costs():
    """Track GPU usage and spending."""


@costs.command()
def summary():
    """Show spending summary for the current billing period."""
    console.print("[dim]Coming soon — Phase 7[/dim]")


@costs.command()
def log():
    """Show detailed session log."""
    console.print("[dim]Coming soon — Phase 7[/dim]")


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
    for s in sources:
        try:
            all_buckets.extend(s.list_buckets())
        except Exception as e:
            console.print(f"[red]✗[/red] {s.name}: {e}")

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
