"""swm setup — install, start, and stop frameworks on running instances."""
from __future__ import annotations

import click

from swm.commands._helpers import console, _instance_for, _framework_url, _probe_url


@click.group()
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
        try:
            install_framework(sess, framework_name, console=console)
        except RuntimeError as e:
            raise click.ClickException(str(e))

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
        try:
            start_framework(sess, framework_name, port=port, console=console)
        except RuntimeError as e:
            raise click.ClickException(str(e))

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
