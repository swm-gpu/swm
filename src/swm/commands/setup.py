"""swm setup — install, start, and stop frameworks on running instances."""
from __future__ import annotations

import click

from swm.commands._helpers import (
    console,
    _instance_for,
    _framework_url,
    _probe_url,
    _open_tunnel,
    complete_pod_id,
    pod_arg_callback,
)


@click.group()
def setup():
    """Install, start, and stop frameworks on a running instance."""


@setup.command(name="storage")
@click.argument("instance_id", required=False, shell_complete=complete_pod_id, callback=pod_arg_callback)
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


@setup.command(name="workspace")
@click.argument("instance_id", required=False, shell_complete=complete_pod_id, callback=pod_arg_callback)
@click.option("--name", "-n", "ws_name", default=None, help="Workspace name (default: pod name)")
@click.option("--bucket", "-b", default=None, help="Bucket spec 'provider:bucket' (default: configured)")
@click.option("--force", is_flag=True, help="Overwrite existing workspace association")
def setup_workspace(instance_id: str, ws_name: str | None, bucket: str | None, force: bool):
    """Attach an object-storage workspace to an existing pod.

    \b
    Performs the full workspace bootstrap in one shot: installs s5cmd,
    configures credentials, pulls (or initializes) the workspace, and
    starts the auto-sync daemon. Persists the pod ↔ workspace ↔ bucket
    association in swm config so subsequent `swm sync pull/push/auto`
    work without overrides.

    \b
    Use this when:
      - You created a pod with --no-storage and now want a workspace.
      - `swm pod create`'s SSH probe timed out and bootstrap was skipped.
      - You want to reattach a pod to a different workspace (with --force).

    \b
    Examples:
      swm setup workspace runpod:abc123                  # ws name = pod name
      swm setup workspace runpod:abc123 -n my-ws         # custom name
      swm setup workspace runpod:abc123 -b b2:my-bucket  # explicit bucket
      swm setup workspace runpod:abc123 --force          # reattach
    """
    from swm import config as _cfg
    from swm.bootstrap import bootstrap_workspace_on_pod
    from swm.remote.ssh import session_from_instance
    from swm.storage import resolve_bucket

    inst = _instance_for(instance_id)
    qid = inst.qualified_id
    raw_id = inst.id

    existing_ws = _cfg.get(f"pods.{raw_id}.workspace")
    existing_storage = _cfg.get(f"pods.{raw_id}.storage")
    if (existing_ws or existing_storage) and not force:
        raise click.ClickException(
            f"Pod {qid} already has a workspace tracked: "
            f"{existing_ws} on {existing_storage}. "
            "Pass --force to overwrite."
        )

    try:
        storage_prov, bucket_name = resolve_bucket(bucket)
    except Exception as exc:
        raise click.ClickException(
            f"Could not resolve storage bucket: {exc}\n"
            "Set credentials with `swm config set <provider>.<key> ...` "
            "or pass -b provider:bucket."
        )

    workspace = ws_name or inst.name
    if not workspace:
        raise click.ClickException(
            "No workspace name available. Pass --name or ensure the pod has a name."
        )

    console.print(
        f"\n[bold]Attaching workspace[/bold] "
        f"[cyan]{workspace}[/cyan] on "
        f"[cyan]{storage_prov.slug}:{bucket_name}[/cyan] to {qid}"
    )

    is_new = True
    try:
        with console.status(
            f"Checking {storage_prov.slug}:{bucket_name}/{workspace}…",
            spinner="dots",
        ):
            objs = storage_prov.ls(bucket_name, prefix=f"{workspace}/")
            is_new = not objs
    except Exception as exc:
        console.print(
            f"  [yellow]⚠ Could not check existing workspace contents: {exc}[/yellow]"
        )
        console.print("  [dim]Treating as new workspace[/dim]")

    if is_new:
        console.print(f"  [dim]New workspace — will initialize empty[/dim]")
    else:
        console.print(
            f"  [dim]Existing workspace found ({len(objs)} objects) — will pull[/dim]"
        )

    _cfg.set_value(f"pods.{raw_id}.workspace", workspace)
    _cfg.set_value(
        f"pods.{raw_id}.storage", f"{storage_prov.slug}:{bucket_name}"
    )

    try:
        with session_from_instance(inst) as sess:
            failed_steps = bootstrap_workspace_on_pod(
                sess,
                storage_prov.slug,
                bucket_name,
                workspace,
                qualified_id=qid,
                is_new=is_new,
                console_obj=console,
            )
    except Exception as exc:
        raise click.ClickException(f"Bootstrap session failed: {exc}")

    if failed_steps:
        console.print(
            "\n[yellow]⚠ Bootstrap incomplete. Re-run the remaining "
            "steps when ready:[/yellow]"
        )
        for label, cmd in failed_steps:
            console.print(f"  [dim]{label}:[/dim]  [bold]{cmd}[/bold]")
        return

    console.print(
        f"\n[green]✓ Workspace attached[/green] "
        f"({workspace} on {storage_prov.slug}:{bucket_name}, auto-sync every 60s)"
    )


@setup.command(name="install")
@click.argument("framework_name")
@click.argument("instance_id", required=False, shell_complete=complete_pod_id, callback=pod_arg_callback)
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

    try:
        fw = get_framework(framework_name)
    except KeyError as e:
        raise click.ClickException(str(e))
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
    console.print(f"  Start:  [bold]swm setup start {fw.name} {instance_id}[/bold]")


@setup.command(name="start")
@click.argument("framework_name")
@click.argument("instance_id", required=False, shell_complete=complete_pod_id, callback=pod_arg_callback)
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

    try:
        fw = get_framework(framework_name)
    except KeyError as e:
        raise click.ClickException(str(e))
    inst = _instance_for(instance_id)

    with session_from_instance(inst) as sess:
        try:
            start_framework(
                sess,
                framework_name,
                port=port,
                console=console,
                qualified_id=inst.qualified_id,
            )
        except RuntimeError as e:
            raise click.ClickException(str(e))

    listen_port = port or (next(iter(fw.ports)) if fw.ports else None)
    if listen_port:
        url = _framework_url(inst, listen_port)
        if not url:
            port_list = fw.ports or {listen_port: "http"}
            console.print(f"  Port(s) {', '.join(str(p) for p in port_list)} not exposed — opening SSH tunnel…")
            tunnelled = _open_tunnel(inst, port_list)
            if tunnelled:
                import time
                time.sleep(2)
                url = f"http://localhost:{listen_port}"
                console.print(f"  [green]✓[/green] Tunnel active → localhost:{', '.join(str(p) for p in tunnelled)}")
        if url:
            with console.status("Checking URL reachability…", spinner="dots"):
                reachable = _probe_url(url, timeout=15)
            if reachable:
                console.print(f"  [green]✓[/green] URL: {url}")
            else:
                console.print(f"  [dim]URL: {url} (not reachable yet — framework may still be loading)[/dim]")


@setup.command(name="stop")
@click.argument("framework_name")
@click.argument("instance_id", required=False, shell_complete=complete_pod_id, callback=pod_arg_callback)
def setup_stop(framework_name: str, instance_id: str):
    """Stop a framework on a running instance.

    \b
    Examples:
      swm setup stop comfyui runpod:abc123
    """
    from swm.bootstrap import stop_framework
    from swm.frameworks import get_framework
    from swm.remote.ssh import session_from_instance

    try:
        get_framework(framework_name)
    except KeyError as e:
        raise click.ClickException(str(e))
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
    from rich.panel import Panel
    from swm.frameworks import list_frameworks

    table = Table(title="Available Frameworks")
    table.add_column("Name", style="bold")
    table.add_column("Label")
    table.add_column("Category")
    table.add_column("Ports")
    table.add_column("Description")

    for fw in list_frameworks():
        ports = ", ".join(f"{p}/{t}" for p, t in fw.ports.items()) if fw.ports else "—"
        table.add_row(fw.name, fw.label, fw.category, ports, fw.description or "—")

    console.print(table)

    console.print(Panel(
        "[bold]LLM Chat[/bold] — start chatting with open-source models\n\n"
        "  [green]Casual / single-GPU:[/green]\n"
        "    swm setup install ollama <id>\n"
        "    swm setup install open-webui <id>\n\n"
        "  [cyan]Fast / multi-GPU (4x H200, 70B+ models):[/cyan]\n"
        "    swm setup install vllm <id>\n"
        "    swm setup install open-webui <id>\n\n"
        "  Then start both and open port 8080 in your browser.\n"
        "  [dim]vLLM auto-detects all GPUs and uses tensor parallelism.[/dim]",
        title="Recommended Setups",
        border_style="blue",
    ))


@setup.command(hidden=True)
@click.argument("instance_id", required=False, shell_complete=complete_pod_id, callback=pod_arg_callback)
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
@click.argument("instance_id", required=False, shell_complete=complete_pod_id, callback=pod_arg_callback)
def swarmui(instance_id: str):
    """Install SwarmUI (alias for 'swm setup install swarmui')."""
    from swm.bootstrap import install_framework
    from swm.remote.ssh import session_from_instance

    inst = _instance_for(instance_id)
    with session_from_instance(inst) as sess:
        install_framework(sess, "swarmui")
    console.print("\n[green]✓ SwarmUI installed[/green]")
