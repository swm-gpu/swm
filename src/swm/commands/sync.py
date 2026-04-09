"""swm sync — sync files between cloud storage and running instances."""
from __future__ import annotations

import click

from swm import config as cfg
from swm.providers import resolve_instance
from swm.commands._helpers import console, _instance_for, _preflight_pull


@click.group()
def sync():
    """Sync files between cloud storage and running instances."""


@sync.command()
@click.argument("instance_id")
@click.argument("path", default="")
@click.option("--bucket", "-b", default=None, help="Override bucket (provider:bucket)")
@click.option("--dest", "-d", default="/workspace", help="Destination on pod")
@click.option("--exclude", "-x", multiple=True, help="Glob pattern to exclude (repeatable)")
@click.option("--force", "-f", is_flag=True, help="Kill any running transfer and start fresh")
def pull(instance_id: str, path: str, bucket: str | None, dest: str, exclude: tuple[str, ...], force: bool):
    """Pull workspace from cloud storage to a running instance.

    \b
    Defaults to the pod's tracked workspace. Always non-destructive
    (skips existing files, never deletes).

    \b
    Examples:
      swm sync pull runpod:abc123                          # full workspace
      swm sync pull runpod:abc123 ComfyUI/models/          # subfolder only
      swm sync pull runpod:abc123 -x '.cache/*' -x 'venv/*'
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

    all_excludes = list(exclude) + (extra_excludes or [])

    with session_from_instance(inst) as sess:
        workspace_pull(
            sess, remote, bucket_name, ws,
            dest=dest, extra_excludes=all_excludes or None,
            total_bytes=total_bytes, total_files=total_files,
            force=force,
        )

    console.print("\n[green]✓ Pull complete[/green]")


@sync.command()
@click.argument("instance_id")
@click.argument("path", default="/workspace")
@click.option("--bucket", "-b", default=None, help="Override bucket (provider:bucket)")
@click.option("--dest", "-d", default="", help="Override destination path inside bucket")
@click.option("--exclude", "-x", multiple=True, help="Glob pattern to exclude (repeatable)")
@click.option("--force", "-f", is_flag=True, help="Kill any running transfer and start fresh")
def push(instance_id: str, path: str, bucket: str | None, dest: str, exclude: tuple[str, ...], force: bool):
    """Push workspace from a running instance to cloud storage.

    \b
    Defaults to the pod's tracked workspace. Always non-destructive
    (uploads new/changed files only, never deletes).

    \b
    Examples:
      swm sync push runpod:abc123                       # full /workspace
      swm sync push runpod:abc123 /workspace/output     # subfolder
      swm sync push runpod:abc123 -x '.cache/*' -x 'venv/*'
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
        workspace_push(
            sess, remote, bucket_name, ws, src=path,
            extra_excludes=list(exclude) or None, force=force,
        )

    console.print("\n[green]✓ Push complete[/green]")


@sync.command(name="watch")
@click.argument("instance_id")
@click.option("--stop", is_flag=True, help="Stop the watcher instead of starting it")
def sync_watch(instance_id: str, stop: bool):
    """Start (or stop) the filesystem change watcher on an instance.

    \b
    The watcher tracks file changes in /workspace/ so that subsequent
    pushes only upload modified files — no scanning required.

    \b
    Examples:
      swm sync watch runpod:abc123          # start watcher
      swm sync watch runpod:abc123 --stop   # stop watcher
    """
    from swm.bootstrap import start_watcher, stop_watcher
    from swm.remote.ssh import session_from_instance

    inst = _instance_for(instance_id)

    with session_from_instance(inst) as sess:
        if stop:
            stop_watcher(sess)
            console.print("[green]✓ Watcher stopped[/green]")
        else:
            ok = start_watcher(sess)
            if ok:
                console.print("[green]✓ Watcher running[/green]")
            else:
                console.print("[red]✗ Failed to start watcher — is inotify-tools installed?[/red]")
                console.print("  Run: swm run <pod> 'apt-get install -y inotify-tools'")


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
