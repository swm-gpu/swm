"""swm sync — sync files between cloud storage and running instances."""
from __future__ import annotations

import click

from swm import config as cfg
from swm.providers import resolve_instance
from swm.commands._helpers import (
    console,
    _instance_for,
    _preflight_pull,
    complete_pod_id,
    pod_arg_callback,
)


@click.group()
def sync():
    """Sync files between cloud storage and running instances."""


@sync.command()
@click.argument("instance_id", required=False, shell_complete=complete_pod_id, callback=pod_arg_callback)
@click.argument("path", default="")
@click.option("--bucket", "-b", default=None, help="Override bucket (provider:bucket)")
@click.option("--dest", "-d", default="/workspace", help="Destination on pod")
@click.option("--exclude", "-x", multiple=True, help="Glob pattern to exclude (repeatable)")
@click.option("--force", "-f", is_flag=True, help="Kill any running transfer and start fresh")
@click.option("--tar", "use_tar", is_flag=True, help="Pull a tarball archive (pushed with --tar)")
def pull(instance_id: str, path: str, bucket: str | None, dest: str, exclude: tuple[str, ...], force: bool, use_tar: bool):
    """Pull workspace from cloud storage to a running instance.

    \b
    Defaults to the pod's tracked workspace. Always non-destructive
    (skips existing files, never deletes).

    \b
    Use --tar to pull a workspace that was pushed with --tar.
    Downloads the tarball and extracts in one step.

    \b
    Examples:
      swm sync pull runpod:abc123                          # full workspace
      swm sync pull runpod:abc123 --tar                    # pull tarball
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

    if use_tar:
        from swm.sync import tar_pull

        with session_from_instance(inst) as sess:
            tar_pull(sess, remote, bucket_name, ws, dest=dest, force=force)

        console.print("\n[green]✓ Pull complete[/green]")
        return

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
@click.argument("instance_id", required=False, shell_complete=complete_pod_id, callback=pod_arg_callback)
@click.argument("path", default="/workspace")
@click.option("--bucket", "-b", default=None, help="Override bucket (provider:bucket)")
@click.option("--dest", "-d", default="", help="Override destination path inside bucket")
@click.option("--exclude", "-x", multiple=True, help="Glob pattern to exclude (repeatable)")
@click.option("--force", "-f", is_flag=True, help="Kill any running transfer and start fresh")
@click.option("--tar", "use_tar", is_flag=True, help="Pack into a tarball before uploading (faster for many small files)")
@click.option("--delete", is_flag=True, help="Also delete files from storage that were deleted locally (requires watcher)")
def push(instance_id: str, path: str, bucket: str | None, dest: str, exclude: tuple[str, ...], force: bool, use_tar: bool, delete: bool):
    """Push workspace from a running instance to cloud storage.

    \b
    Defaults to the pod's tracked workspace. Uploads new and changed
    files only.

    \b
    Use --delete to propagate local deletions to storage. Files you
    removed on the pod since the last push will also be deleted from
    the bucket. Requires the filesystem watcher to be running.

    \b
    Use --tar for workspaces with many small files (100k+). Packs
    into a single compressed tarball, turning 600k S3 API calls
    into one.

    \b
    Examples:
      swm sync push runpod:abc123                       # full /workspace
      swm sync push runpod:abc123 --delete              # push + propagate deletions
      swm sync push runpod:abc123 --tar                 # tar mode (many small files)
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
            tar=use_tar, delete=delete,
        )

    console.print("\n[green]✓ Push complete[/green]")


@sync.command(name="watch")
@click.argument("instance_id", required=False, shell_complete=complete_pod_id, callback=pod_arg_callback)
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
                console.print(
                    f"  Run: swm run {inst.qualified_id} 'apt-get install -y inotify-tools'"
                )


@sync.command(name="auto")
@click.argument("instance_id", required=False, shell_complete=complete_pod_id, callback=pod_arg_callback)
@click.option("--stop", is_flag=True, help="Stop the auto-sync daemon")
@click.option("--status", "show_status", is_flag=True, help="Show status and recent log")
@click.option("--interval", "-i", default=60, type=int, help="Sync interval in seconds (default: 60)")
@click.option("--bucket", "-b", default=None, help="Override bucket (provider:bucket)")
@click.option("--dest", "-d", default="", help="Override destination path inside bucket")
@click.option("--force", is_flag=True, help="Start without a prior successful pull/push (DANGEROUS: local deletions will propagate)")
def sync_auto(instance_id: str, stop: bool, show_status: bool, interval: int,
              bucket: str | None, dest: str, force: bool):
    """Start a background daemon that auto-syncs /workspace to storage.

    \b
    Reads the filesystem watcher log every INTERVAL seconds and uploads
    changed files + deletes removed files. No manual push needed.

    \b
    Safety: refuses to start unless the pod and bucket are known to be
    in sync (a prior `swm sync pull` or `swm sync push` succeeded).
    Without that signal, a stray local deletion would propagate to
    storage and wipe the remote copy. Use --force to override.

    \b
    Examples:
      swm sync auto runpod:abc123                  # start with 60s interval
      swm sync auto runpod:abc123 -i 30            # 30s interval
      swm sync auto runpod:abc123 --status         # check daemon state + log
      swm sync auto runpod:abc123 --stop           # stop the daemon
      swm sync auto runpod:abc123 --force          # bypass safety check
    """
    from swm.sync.autosync import AutosyncUnsafeError
    from swm.bootstrap import (
        start_autosync,
        stop_autosync,
        is_autosync_alive,
        autosync_status,
    )
    from swm.remote.ssh import session_from_instance

    inst = _instance_for(instance_id)

    with session_from_instance(inst) as sess:
        if stop:
            stop_autosync(sess)
            console.print("[green]✓ Auto-sync stopped[/green]")
            return

        if show_status:
            alive, tail = autosync_status(sess)
            state = "[green]running[/green]" if alive else "[red]stopped[/red]"
            console.print(f"\n[bold]Auto-sync:[/bold] {state}")
            if tail.strip():
                console.print("\n[bold]Recent log:[/bold]")
                console.print(f"[dim]{tail.rstrip()}[/dim]")
            return

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

        if is_autosync_alive(sess):
            console.print("[yellow]Auto-sync already running.[/yellow] "
                          "Use --stop to stop it first.")
            return

        try:
            ok = start_autosync(
                sess, remote, bucket_name, ws, interval=interval, force=force,
            )
        except AutosyncUnsafeError as exc:
            raise click.ClickException(str(exc))

        if ok:
            console.print(
                f"[green]✓ Auto-sync started[/green] "
                f"({remote}:{bucket_name}/{ws}, every {interval}s)"
            )
            console.print(
                f"  [dim]View log:  swm sync auto {inst.qualified_id} --status[/dim]"
            )
            console.print(
                f"  [dim]Stop:      swm sync auto {inst.qualified_id} --stop[/dim]"
            )
        else:
            console.print("[red]✗ Failed to start auto-sync[/red]")
            console.print("  [dim]Check that inotify-tools is installed on the pod[/dim]")


@sync.command(name="status")
@click.argument("instance_id", required=False, shell_complete=complete_pod_id, callback=pod_arg_callback)
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
