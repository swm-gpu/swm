"""swm pod — manage cloud GPU instances across providers."""
from __future__ import annotations

import click
from rich.table import Table

from swm import config as cfg
from swm.providers import (
    get_configured_providers,
    get_provider,
    resolve_instance,
    PROVIDER_SLUGS,
)
from swm.providers.base import InstanceStatus
from swm.commands._helpers import (
    console,
    _instance_for,
    complete_pod_id,
    pod_arg_callback,
    clear_active_pod,
    set_active_pod,
)


@click.group()
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

    _prune_stale_pods(live_ids={i.id for i in all_instances})


def _prune_stale_pods(live_ids: set[str] | None = None) -> int:
    """Remove config entries for pods that no longer exist on any provider.

    If *live_ids* is given, skip the API call and use these as the set of
    known-live instance IDs.  Returns the number of entries removed.
    """
    pods = cfg.get("pods", {}) or {}
    if not pods:
        return 0

    if live_ids is None:
        live_ids = set()
        for p in get_configured_providers():
            try:
                for inst in p.list_instances():
                    live_ids.add(inst.id)
            except Exception:
                return 0

    removed = 0
    for pod_id in list(pods):
        if pod_id not in live_ids:
            try:
                from swm.costs.tracker import record_stop

                provider_slug = (pods[pod_id] or {}).get("provider", "")
                if provider_slug:
                    record_stop(pod_id, provider_slug)
            except Exception:
                pass
            cfg.delete(f"pods.{pod_id}")
            removed += 1
    return removed


@pod.command(name="prune")
def pod_prune():
    """Remove config entries for pods that no longer exist."""
    with console.status("Checking providers…", spinner="dots"):
        removed = _prune_stale_pods()
    if removed:
        console.print(f"[green]✓[/green] Removed {removed} stale pod(s) from config.")
    else:
        console.print("[dim]No stale pods found.[/dim]")


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
@click.option(
    "--cuda", "cuda",
    default=None,
    help="Auto-pick newest provider image matching this CUDA major.minor "
         "(e.g. 12.8). Ignored if --image is set.",
)
@click.option("--cloud-type", default="SECURE", help="RunPod cloud type: SECURE, COMMUNITY, ALL")
@click.option("--ports", default="22/tcp,8888/http,8188/http", help="Ports to expose")
@click.option("--gpu-count", default=1, type=int, help="Number of GPUs")
@click.option("--region", default=None, help="Datacenter/region ID (e.g. US-CA-2)")
@click.option(
    "--lifecycle",
    default=None,
    type=click.Choice(["manual", "remind", "auto-stop", "auto-down"], case_sensitive=False),
    help="Idle lifecycle policy for this pod.",
)
@click.option("--idle-timeout", default=None, type=int, help="Idle timeout in minutes for lifecycle guard")
@click.option("--exclude", "-x", multiple=True, help="Glob pattern to exclude from pull (repeatable)")
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
    cuda: str | None,
    cloud_type: str,
    ports: str,
    gpu_count: int,
    region: str | None,
    lifecycle: str | None,
    idle_timeout: int | None,
    exclude: tuple[str, ...],
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
    ws_is_new = False
    from swm.guard import _coalesce_int

    guard_defaults = cfg.get("guard.defaults", {}) or {}
    lifecycle_mode = lifecycle or guard_defaults.get("mode")
    lifecycle_idle = _coalesce_int(
        idle_timeout,
        guard_defaults.get("idle_timeout_minutes"),
        default=60,
    )

    if not no_storage:
        try:
            from swm.storage import resolve_bucket

            storage_prov, bucket_name = resolve_bucket(bucket)
            if workspace:
                ws_name = workspace
                ws_action = f"restore [cyan]{workspace}[/cyan]"
            else:
                ws_name = name
                ws_action = f"new [cyan]{ws_name}[/cyan]"
                ws_is_new = True
        except Exception as exc:
            console.print(
                f"[yellow]⚠ Workspace disabled — could not resolve storage "
                f"bucket: {exc}[/yellow]"
            )
            console.print(
                "  [dim]Configure storage, then bootstrap later with "
                "[bold]swm setup storage <pod>[/bold] and "
                "[bold]swm sync pull <pod>[/bold]. "
                "Pass --no-storage to silence this warning.[/dim]"
            )

    if not image and cuda:
        from swm.images import resolve_image

        try:
            resolved = resolve_image(provider, cuda)
        except Exception as exc:
            raise click.ClickException(f"Could not look up images: {exc}")
        if not resolved:
            raise click.ClickException(
                f"No image found for {provider} with CUDA {cuda}. "
                f"Run [bold]swm images list -p {provider}[/bold] to see options, "
                "or pass --image explicitly."
            )
        image = resolved
        console.print(
            f"[dim]Resolved --cuda {cuda} → "
            f"[cyan]{image}[/cyan][/dim]"
        )

    if image and gpu:
        from swm.cuda import min_cuda_for, cuda_at_least
        from swm.images import parse_image_cuda

        gpu_min = min_cuda_for(gpu)
        img_cuda = parse_image_cuda(image)
        if gpu_min and img_cuda and not cuda_at_least(img_cuda, gpu_min):
            console.print(
                f"[yellow]⚠ Image CUDA {img_cuda} is below {gpu}'s minimum "
                f"({gpu_min}). The pod may fail to start GPU workloads.[/yellow]"
            )

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
    if lifecycle_mode and lifecycle_mode != "manual":
        console.print(f"  Lifecycle:  {lifecycle_mode} after {lifecycle_idle}m idle")
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

    try:
        from swm.costs.tracker import record_start
        from swm.costs.budget import check_budget

        record_start(inst, workspace=ws_name)
        warning = check_budget(provider)
        if warning:
            for line in warning.splitlines():
                console.print(f"  [yellow]⚠ {line}[/yellow]")
    except Exception:
        pass

    from swm.bootstrap import wait_for_ssh
    ssh_ok = False
    console.print()
    try:
        inst = wait_for_ssh(p, inst.id)
        ssh_ok = True
    except TimeoutError as e:
        console.print(f"[yellow]⚠ {e}[/yellow]")

    cfg.set_value(f"pods.{inst.id}.provider", provider)
    cfg.set_value(f"pods.{inst.id}.name", name)
    set_active_pod(inst.id)
    policy = None
    if lifecycle_mode:
        from swm.guard import set_policy

        policy = set_policy(
            inst.id,
            mode=lifecycle_mode,
            idle_timeout_minutes=int(lifecycle_idle),
        )

    failed_steps: list[tuple[str, str]] = []
    qid = inst.qualified_id

    if ws_name and storage_prov and bucket_name:
        cfg.set_value(f"pods.{inst.id}.workspace", ws_name)
        cfg.set_value(
            f"pods.{inst.id}.storage", f"{storage_prov.slug}:{bucket_name}"
        )

    if ws_name and storage_prov and bucket_name and not ssh_ok:
        failed_steps = [
            ("Storage configuration", f"swm setup storage {qid}"),
            ("Workspace pull", f"swm sync pull {qid}"),
            ("Auto-sync start", f"swm sync auto {qid}"),
        ]

    if ssh_ok and ws_name and storage_prov and bucket_name:
        console.print(f"\n[bold]Bootstrapping storage over SSH…[/bold]")
        from swm.remote.ssh import session_from_instance
        from swm.bootstrap import bootstrap_workspace_on_pod

        try:
            with session_from_instance(inst) as sess:
                failed_steps = bootstrap_workspace_on_pod(
                    sess,
                    storage_prov.slug,
                    bucket_name,
                    ws_name,
                    qualified_id=qid,
                    is_new=ws_is_new,
                    extra_excludes=list(exclude) or None,
                    console_obj=console,
                )
        except Exception as exc:
            console.print(
                f"[yellow]⚠ Bootstrap session failed: {exc}[/yellow]"
            )
            if not failed_steps:
                failed_steps = [
                    ("Storage configuration", f"swm setup storage {qid}"),
                    ("Workspace pull", f"swm sync pull {qid}"),
                    ("Auto-sync start", f"swm sync auto {qid}"),
                ]

    if failed_steps:
        console.print(
            "\n[yellow]⚠ Bootstrap incomplete. Re-run the remaining "
            "steps when ready:[/yellow]"
        )
        for label, cmd in failed_steps:
            console.print(f"  [dim]{label}:[/dim]  [bold]{cmd}[/bold]")

    if ssh_ok and policy and policy.enabled:
        from swm.guard import ensure_remote_guard

        try:
            with console.status("Starting lifecycle guard…", spinner="dots"):
                ensure_remote_guard(inst, policy)
            console.print(
                f"  [dim]Lifecycle guard running: {policy.mode} after "
                f"{policy.idle_timeout_minutes}m idle[/dim]"
            )
        except Exception as e:
            console.print(f"[yellow]⚠ Failed to start lifecycle guard: {e}[/yellow]")

    if policy and policy.enabled:
        from swm.guard import ensure_local_daemon

        if ensure_local_daemon():
            console.print("  [dim]Local guard daemon running[/dim]")

    if ssh_ok:
        console.print(f"\n[bold green]✓ Pod ready![/bold green]")
    else:
        console.print(f"\n[bold yellow]⚠ Pod created but SSH is not ready yet.[/bold yellow]")
        console.print("  The instance may still be loading (e.g. Docker image build).")
        console.print(f"  Check status:  [bold]swm pod status {inst.qualified_id}[/bold]")
        console.print(f"  Try SSH later:  [bold]swm ssh {inst.qualified_id}[/bold]")

    console.print(f"  ID:        {inst.qualified_id}")
    if inst.cost_per_hr:
        console.print(f"  Cost:      ${inst.cost_per_hr:.2f}/hr")
    if ssh_ok and inst.ssh_command:
        console.print(f"  SSH:       {inst.ssh_command}")
    if policy and policy.enabled:
        console.print(f"  Lifecycle: {policy.mode} after {policy.idle_timeout_minutes}m idle")
    if ws_name and storage_prov:
        console.print(f"  Workspace: {ws_name} on {storage_prov.slug}:{bucket_name}")
        autosync_pending = any(label == "Auto-sync start" for label, _ in failed_steps)
        if autosync_pending:
            console.print(f"  Auto-sync: [yellow]pending — see steps above[/yellow]")
        else:
            console.print(f"  Auto-sync: every 60s")
    elif no_storage:
        console.print(f"  Workspace: [dim]disabled (--no-storage)[/dim]")
        console.print(f"  Auto-sync: [dim]not available without storage[/dim]")
    else:
        console.print(f"  Workspace: [dim]not configured[/dim]")
        console.print(f"  Auto-sync: [dim]not available without workspace[/dim]")
    console.print(
        f"\n  Shut down:  [bold]swm pod down {inst.qualified_id}[/bold]"
    )


@pod.command()
@click.argument("instance_id", required=False, shell_complete=complete_pod_id, callback=pod_arg_callback)
def start(instance_id: str):
    """Start a stopped instance and wait until SSH is ready.

    \b
    Polls the provider until the instance is running, then probes SSH
    connectivity — same readiness checks as 'swm pod create'.
    """
    try:
        with console.status("Sending start request…", spinner="dots"):
            provider, raw_id = resolve_instance(instance_id)
            provider.start_instance(raw_id)
    except Exception as e:
        raise click.ClickException(str(e))

    console.print(f"[bold]Starting {provider.name} instance {raw_id}…[/bold]")

    from swm.bootstrap import wait_for_ssh

    try:
        inst = wait_for_ssh(provider, raw_id)
    except TimeoutError as e:
        raise click.ClickException(str(e))

    console.print(f"\n[bold green]✓ Pod ready![/bold green]")
    console.print(f"  ID:   {inst.qualified_id}")
    if inst.cost_per_hr:
        console.print(f"  Cost: ${inst.cost_per_hr:.2f}/hr")
    if inst.ssh_command:
        console.print(f"  SSH:  {inst.ssh_command}")

    try:
        from swm.guard import ensure_remote_guard, get_policy, ensure_local_daemon

        policy = get_policy(raw_id)
        if policy.enabled:
            with console.status("Starting lifecycle guard…", spinner="dots"):
                ensure_remote_guard(inst, policy)
            console.print(f"  Lifecycle: {policy.mode} after {policy.idle_timeout_minutes}m idle")
            ensure_local_daemon()
    except Exception as e:
        console.print(f"  [yellow]⚠ Lifecycle guard not started: {e}[/yellow]")

    try:
        from swm.costs.tracker import record_start
        from swm.costs.budget import check_budget

        record_start(inst)
        warning = check_budget(provider.slug)
        if warning:
            for line in warning.splitlines():
                console.print(f"  [yellow]⚠ {line}[/yellow]")
    except Exception:
        pass


@pod.command()
@click.argument("instance_id", required=False, shell_complete=complete_pod_id, callback=pod_arg_callback)
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
@click.argument("instance_id", required=False, shell_complete=complete_pod_id, callback=pod_arg_callback)
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

    cfg.delete(f"pods.{raw_id}")
    clear_active_pod(if_matches=raw_id)


@pod.command()
@click.argument("instance_id", required=False, shell_complete=complete_pod_id, callback=pod_arg_callback)
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
    try:
        from swm.guard import get_policy

        policy = get_policy(raw_id)
        if policy.enabled:
            console.print(f"  Lifecycle:  {policy.mode} after {policy.idle_timeout_minutes}m idle")
    except Exception:
        pass


@pod.command(name="down")
@click.argument("instance_id", required=False, shell_complete=complete_pod_id, callback=pod_arg_callback)
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation")
@click.option("--no-sync", is_flag=True, help="Skip workspace push before termination")
@click.option("--exclude", "-x", multiple=True, help="Glob pattern to exclude from push (repeatable)")
def pod_down(instance_id: str, yes: bool, no_sync: bool, exclude: tuple[str, ...]):
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
            from swm.sync import stop_autosync, stop_watcher

            slug, bucket = meta["storage"].split(":", 1)
            ws = meta["workspace"]

            console.print(f"\n[bold]Pushing workspace to {meta['storage']}...[/bold]")
            try:
                with session_from_instance(inst) as sess:
                    try:
                        stop_autosync(sess)
                        stop_watcher(sess)
                    except Exception:
                        pass
                    workspace_push(
                        sess, slug, bucket, ws,
                        extra_excludes=list(exclude) or None,
                    )
                    sess.exec(
                        "rm -rf /workspace/* /workspace/.[!.]* /workspace/..?* 2>/dev/null || true",
                        stream=False,
                    )
                console.print("[green]✓ Workspace synced & wiped on pod[/green]")
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
    clear_active_pod(if_matches=raw_id)

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
    from swm.cli import gpus
    ctx.invoke(gpus, gpu=gpu, provider=provider)
